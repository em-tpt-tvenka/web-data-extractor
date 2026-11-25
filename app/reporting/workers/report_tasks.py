"""Background report generation on the existing Celery/Redis infrastructure.

The pipeline (summary -> insights -> files) runs inside ``run_report`` with
explicit status checkpoints persisted to the ``reports`` document, so a Celery
retry resumes from a known state and completed scraping/NLP is never repeated
needlessly. No new broker is introduced.
"""

from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.reporting.services.report_service import ReportService
from app.storage.database import ensure_indexes
from app.workers.celery_app import celery_app

_log = get_logger("reporting.worker")


async def _run(report_id: str) -> None:
    await ensure_indexes()
    await ReportService().run_report(report_id)


@celery_app.task(
    name="reporting.run_report",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_report_task(self, report_id: str) -> str:
    _log.info("event=report_task_start report_id=%s task_id=%s", report_id, self.request.id)
    try:
        asyncio.run(_run(report_id))
    except Exception as exc:  # pragma: no cover - retried
        _log.exception("event=report_task_error report_id=%s", report_id)
        raise self.retry(exc=exc)
    _log.info("event=report_task_end report_id=%s", report_id)
    return report_id
