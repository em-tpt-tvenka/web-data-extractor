from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pydantic import ValidationError

from app.core import identifiers
from app.core.config import settings
from app.core.exceptions import (
    JobNotFoundError,
    PlatformNotSupportedError,
    RequestValidationError,
    ScrapingError,
)
from app.core.logging import get_logger
from app.platforms import registry
from app.schemas.scraping import (
    JobCreatedResponse,
    JobStatus,
    JobStatusResponse,
    ScrapeJobRequest,
)
from app.storage.repositories.job_repository import JobRepository

_log = get_logger("job_manager")

# Keep strong references to in-flight inline tasks so they are not garbage
# collected before completion.
_INLINE_TASKS: set[asyncio.Task] = set()


class JobManager:
    def __init__(self) -> None:
        self.jobs = JobRepository()

    async def create_job(self, request: ScrapeJobRequest) -> JobCreatedResponse:
        platform = request.platform.value
        if not registry.is_supported(platform):
            raise PlatformNotSupportedError(f"platform '{platform}' is not supported")

        adapter = registry.get_adapter(platform)
        try:
            adapter.validate_options(request.options_for_platform())
        except ValidationError as exc:
            raise RequestValidationError(
                f"invalid platform_options for {platform}: {exc.errors()}"
            )

        job_id = identifiers.execution_id(platform)
        created_at = datetime.now(timezone.utc)
        await self.jobs.create(job_id, platform, request.model_dump(mode="json"))

        # Mark queued before dispatch so the status is persisted first; the
        # executor then moves it to running/completed.
        await self.jobs.mark_status(job_id, JobStatus.queued)
        try:
            self._dispatch(job_id)
        except Exception as exc:
            await self.jobs.mark_status(job_id, JobStatus.failed, error=f"enqueue failed: {exc}")
            raise ScrapingError(f"could not enqueue job: {exc}")

        _log.info(
            "event=job_created job_id=%s platform=%s executor=%s",
            job_id, platform, settings.job_executor,
        )
        return JobCreatedResponse(
            job_id=job_id, status=JobStatus.queued,
            platform=request.platform, created_at=created_at,
        )

    async def get_job(self, job_id: str) -> JobStatusResponse:
        doc = await self.jobs.get(job_id)
        if doc is None:
            raise JobNotFoundError(f"job '{job_id}' not found")
        return JobStatusResponse(
            job_id=doc["job_id"],
            status=doc["status"],
            platform=doc["platform"],
            created_at=doc["created_at"],
            started_at=doc.get("started_at"),
            finished_at=doc.get("finished_at"),
            products_scraped=doc.get("products_scraped", 0),
            records_written=doc.get("records_written", 0),
            exports=doc.get("exports"),
            execution_meta=doc.get("execution_meta"),
            error=doc.get("error"),
        )

    @staticmethod
    def _dispatch(job_id: str) -> None:
        if settings.job_executor == "inline":
            JobManager._dispatch_inline(job_id)
            return
        from app.workers.tasks import run_scrape_job

        run_scrape_job.delay(job_id)

    @staticmethod
    def _dispatch_inline(job_id: str) -> None:
        """Run the job in-process on the current event loop (no broker)."""
        task = asyncio.create_task(JobManager._run_inline(job_id))
        _INLINE_TASKS.add(task)
        task.add_done_callback(_INLINE_TASKS.discard)

    @staticmethod
    async def _run_inline(job_id: str) -> None:
        from app.orchestrator.engine import Orchestrator

        try:
            await Orchestrator().run_job(job_id)
        except Exception:  # pragma: no cover - defensive; orchestrator self-reports
            _log.exception("event=inline_job_crashed job_id=%s", job_id)
