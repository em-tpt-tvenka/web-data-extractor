import asyncio

from app.core.logging import get_logger
from app.orchestrator.engine import Orchestrator
from app.storage.database import ensure_indexes
from app.workers.celery_app import celery_app

_log = get_logger("worker")


async def _run(job_id: str) -> None:
    await ensure_indexes()
    await Orchestrator().run_job(job_id)


@celery_app.task(name="scraping.run_job", bind=True)
def run_scrape_job(self, job_id: str) -> str:
    _log.info("event=task_start job_id=%s task_id=%s", job_id, self.request.id)
    asyncio.run(_run(job_id))
    _log.info("event=task_end job_id=%s", job_id)
    return job_id
