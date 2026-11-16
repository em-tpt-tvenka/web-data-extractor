from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas.scraping import JobStatus
from app.storage.database import get_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobRepository:
    @property
    def _col(self):
        return get_db().jobs

    async def create(self, job_id: str, platform: str, request: dict) -> dict:
        doc = {
            "job_id": job_id,
            "platform": platform,
            "status": JobStatus.pending.value,
            "request": request,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "products_scraped": 0,
            "records_written": 0,
            "quality": None,
            "exports": None,
            "execution_meta": None,
            "error": None,
        }
        await self._col.insert_one(doc)
        return doc

    async def get(self, job_id: str) -> dict | None:
        return await self._col.find_one({"job_id": job_id}, {"_id": 0})

    async def update(self, job_id: str, **fields: Any) -> None:
        if fields:
            await self._col.update_one({"job_id": job_id}, {"$set": fields})

    async def mark_status(self, job_id: str, status: JobStatus, **extra: Any) -> None:
        await self.update(job_id, status=status.value, **extra)
