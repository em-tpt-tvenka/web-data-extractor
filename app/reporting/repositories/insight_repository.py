"""Persistence for ``report_insights``."""

from __future__ import annotations

from app.reporting.repositories import REPORT_INSIGHTS
from app.reporting.schemas.report_insight import Insight
from app.storage.database import get_db


class InsightRepository:
    @property
    def _col(self):
        return get_db()[REPORT_INSIGHTS]

    async def save_many(self, insights: list[Insight]) -> int:
        if not insights:
            return 0
        docs = [i.to_mongo() for i in insights]
        await self._col.insert_many(docs)
        return len(docs)

    async def for_report(self, report_id: str) -> list[dict]:
        cursor = self._col.find({"report_id": report_id})
        return [doc async for doc in cursor]

    async def delete_for_report(self, report_id: str) -> int:
        res = await self._col.delete_many({"report_id": report_id})
        return res.deleted_count
