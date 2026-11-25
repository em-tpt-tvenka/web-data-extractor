"""Persistence for ``report_summaries``."""

from __future__ import annotations

from typing import Any

from app.reporting.repositories import REPORT_SUMMARIES
from app.reporting.schemas.report_summary import ReportSummary
from app.storage.database import get_db


class SummaryRepository:
    @property
    def _col(self):
        return get_db()[REPORT_SUMMARIES]

    async def save(self, summary: ReportSummary) -> str:
        doc = summary.to_mongo()
        await self._col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return doc["_id"]

    async def get(self, summary_id: str) -> dict | None:
        return await self._col.find_one({"_id": summary_id})

    async def find_reusable(
        self, summary_key: str, tenant_id: str | None, project_id: str | None
    ) -> dict | None:
        """Return the newest summary with a matching reuse key within the same
        tenant/project scope, or ``None``."""
        query: dict[str, Any] = {"summary_key": summary_key}
        query["tenant_id"] = tenant_id
        query["project_id"] = project_id
        return await self._col.find_one(query, sort=[("created_at", -1)])

    async def set_report_id(self, summary_id: str, report_id: str) -> None:
        await self._col.update_one({"_id": summary_id}, {"$set": {"report_id": report_id}})
