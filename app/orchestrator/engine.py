from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.config import settings
from app.core.exceptions import ScrapingError
from app.core.logging import get_logger
from app.core.periods import report_scope_ids
from app.orchestrator import result_aggregator
from app.platforms.base import ScrapeResult
from app.platforms.registry import get_adapter
from app.processing import normalizer, quality_checker
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import JobStatus, ScrapeJobRequest
from app.storage.database import get_db
from app.storage.incremental import IncrementalStore
from app.storage.local_storage import LocalStorage
from app.storage.repositories.collections_repository import CollectionsRepository
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
        self.collections = CollectionsRepository()

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

            store = await self._incremental_store(adapter, request, job_id)
            try:
                if store is not None:
                    result = await adapter.scrape(request, options, run_id=job_id, hooks=store)
                else:
                    result = await adapter.scrape(request, options, run_id=job_id)
            finally:
                storage = await store.close() if store is not None else None
            result.raw_products = result_aggregator.dedupe_products(result.raw_products)

            if store is not None:
                records, todo = store.records_for(result.raw_products)
                if todo:  # e.g. products whose incremental mapping did not happen
                    records += await asyncio.to_thread(
                        adapter.to_records, ScrapeResult(raw_products=todo, warnings=result.warnings,
                                                         run_meta=result.run_meta), request, job_id)
            else:
                records = await asyncio.to_thread(adapter.to_records, result, request, job_id)
            await self._assign_identity(records, request)
            normalizer.finalize(records)
            # Products the incremental store already wrote are not written twice.
            pending = [r for r in records if store is None or r.source.source_item_id not in store.flushed]
            written = await self.records.upsert_many(pending) if pending else 0
            collections = await self._save_collections(job_id, pending) if pending else {}
            if store is not None:
                written += len(store.flushed)
                collections = {"incremental": storage, "final": collections}
                self._merge_storage_stats(result.run_meta, store, storage)
            summary = quality_checker.summarize(records)

            exports = self.local.export_records(
                request.platform.value, job_id, records, label=request.search.product
            )
            if store is not None and exports.get("reviews"):
                store.discard_partial_file()

            await self.jobs.mark_status(
                job_id, JobStatus.completed, finished_at=_now(),
                products_scraped=len(result.raw_products),
                records_written=written, quality=summary, exports=exports,
                execution_meta=result.run_meta, collections=collections,
            )
            _log.info("event=job_completed job_id=%s products=%d records=%d",
                      job_id, len(result.raw_products), written)
        except Exception as exc:
            _log.exception("event=job_failed job_id=%s", job_id)
            await self.jobs.mark_status(
                job_id, JobStatus.failed, finished_at=_now(), error=str(exc)
            )

    async def _incremental_store(self, adapter, request: ScrapeJobRequest, job_id: str):
        """Incremental storage + resume for platforms that support it. A store
        that cannot start (e.g. MongoDB unreachable) falls back to the
        end-of-run write instead of failing the job."""
        if not getattr(adapter, "supports_incremental", False):
            return None
        store = IncrementalStore(
            adapter, request, job_id, db=get_db(), records_repo=self.records,
            collections_repo=self.collections, assign_identity=self._assign_identity,
            export_dir=self.local.platform_dir(request.platform.value),
            batch_size=settings.storage_batch_size, resume_hours=settings.resume_completed_within_hours,
        )
        try:
            await store.start()
        except Exception as exc:
            _log.warning("event=incremental_store_unavailable job_id=%s error=%s", job_id, exc)
            return None
        return store

    @staticmethod
    def _merge_storage_stats(run_meta: dict, store: IncrementalStore, storage: dict) -> None:
        perf = (run_meta or {}).get("performance")
        if not perf:
            return
        for p in perf.get("per_product") or []:
            if p.get("product_id") in store.unique_stored:
                p["unique_reviews_stored"] = store.unique_stored[p["product_id"]]
        perf["unique_reviews_stored"] = storage.get("unique_reviews_stored")
        perf["storage_batches"] = storage.get("batches_written")
        perf["storage_errors"] = storage.get("write_errors")

    async def _save_collections(self, job_id: str, records: list[SocialMentionRecord]) -> dict:
        """Also store the records split into products / retailers / reviews.
        Additive and isolated: a failure here is logged on the job and never
        fails the job or changes the records collection and exports."""
        try:
            return await self.collections.save_scraped_data(records)
        except Exception as exc:
            _log.exception("event=collections_save_failed job_id=%s", job_id)
            return {"error": str(exc)[:300]}

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
