"""Persistence for the ``reports`` collection (job metadata + status)."""

from __future__ import annotations

from typing import Any

from app.reporting.utils.report_helpers import now_iso
from app.reporting.repositories import REPORTS
from app.storage.database import get_db


class ReportRepository:
    @property
    def _col(self):
        return get_db()[REPORTS]

    async def create(self, doc: dict) -> dict:
        doc.setdefault("created_at", now_iso())
        await self._col.insert_one(doc)
        return doc

    async def get(self, report_id: str) -> dict | None:
        return await self._col.find_one({"_id": report_id})

    async def update(self, report_id: str, **fields: Any) -> None:
        if fields:
            await self._col.update_one({"_id": report_id}, {"$set": fields})

    async def set_status(self, report_id: str, status: str, **extra: Any) -> None:
        await self.update(report_id, status=status, **extra)

    async def list(
        self,
        tenant_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query: dict[str, Any] = {}
        if tenant_id is not None:
            query["tenant_id"] = tenant_id
        if project_id is not None:
            query["project_id"] = project_id
        cursor = self._col.find(query).sort("created_at", -1).limit(limit)
        return [doc async for doc in cursor]
