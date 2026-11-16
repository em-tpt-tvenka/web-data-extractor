from __future__ import annotations

from datetime import datetime, timezone

from app.core.exceptions import ScrapingError
from app.core.logging import get_logger
from app.core.periods import report_scope_ids
from app.orchestrator import result_aggregator
from app.platforms.registry import get_adapter
from app.processing import normalizer, quality_checker
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import JobStatus, ScrapeJobRequest
from app.storage.local_storage import LocalStorage
from app.storage.repositories.job_repository import JobRepository
from app.storage.repositories.provisioning_repository import ProvisioningRepository
from app.storage.repositories.record_repository import RecordRepository

_log = get_logger("orchestrator")

def _now() -> datetime:
    return datetime.now(timezone.utc)

class Orchestrator:
    def __init__(self) -> None:
        self.jobs = JobRepository()
        self.records = RecordRepository()
        self.local = LocalStorage()
        self.provisioning = ProvisioningRepository()

    async def run_job(self, job_id: str) -> None:
        job = await self.jobs.get(job_id)
        if job is None:
            _log.warning("event=job_missing job_id=%s", job_id)
            return

        await self.jobs.mark_status(job_id, JobStatus.running, started_at=_now())
        try:
            request = ScrapeJobRequest.model_validate(job["request"])
            adapter = get_adapter(request.platform.value)
            options = adapter.validate_options(request.options_for_platform())

            result = await adapter.scrape(request, options, run_id=job_id)
            result.raw_products = result_aggregator.dedupe_products(result.raw_products)

            records = adapter.to_records(result, request, run_id=job_id)
            await self._assign_identity(records, request)
            normalizer.finalize(records)
            written = await self.records.upsert_many(records)
            summary = quality_checker.summarize(records)

            exports = self.local.export_records(request.platform.value, job_id, records)

            await self.jobs.mark_status(
                job_id, JobStatus.completed, finished_at=_now(),
                products_scraped=len(result.raw_products),
                records_written=written, quality=summary, exports=exports,
                execution_meta=result.run_meta,
            )
            _log.info("event=job_completed job_id=%s products=%d records=%d",
                      job_id, len(result.raw_products), written)
        except Exception as exc:
            _log.exception("event=job_failed job_id=%s", job_id)
            await self.jobs.mark_status(
                job_id, JobStatus.failed, finished_at=_now(), error=str(exc)
            )

    async def _assign_identity(
        self, records: list[SocialMentionRecord], request: ScrapeJobRequest
    ) -> None:
        if not records:
            return

        platform = request.platform.value
        context = await self.provisioning.get_or_create_context()
        tenant_id = (context or {}).get("tenant_id")
        project_id = (context or {}).get("project_id")
        if not tenant_id or not project_id:
            raise ScrapingError("could not resolve tenant/project context")

        scope_ids = report_scope_ids(request.scraping_options.date_range)

        source_ids = [r.source.source_item_id for r in records]
        assigned = await self.records.existing_ids_by_source(tenant_id, platform, source_ids)

        reused = 0
        for record in records:
            sid = record.source.source_item_id
            existing = assigned.get(sid)
            if existing is None:
                assigned[sid] = record.id
            else:
                record.id = existing
                reused += 1
            record.tenant.tenant_id = tenant_id
            record.tenant.project_id = project_id
            record.tenant.report_scope_ids = scope_ids

        _log.info(
            "event=identity_assigned platform=%s tenant_id=%s project_id=%s "
            "scope=%s records=%d reused_ids=%d",
            platform, tenant_id, project_id, scope_ids, len(records), reused,
        )
