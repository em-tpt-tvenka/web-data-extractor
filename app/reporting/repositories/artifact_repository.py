"""Persistence for ``report_artifacts`` (file metadata only; never binaries)."""

from __future__ import annotations

from app.reporting.repositories import REPORT_ARTIFACTS
from app.storage.database import get_db


class ArtifactRepository:
    @property
    def _col(self):
        return get_db()[REPORT_ARTIFACTS]

    async def save(self, doc: dict) -> str:
        await self._col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return doc["_id"]

    async def for_report(self, report_id: str) -> list[dict]:
        cursor = self._col.find({"report_id": report_id})
        return [doc async for doc in cursor]

    async def get_by_type(self, report_id: str, file_type: str) -> dict | None:
        return await self._col.find_one({"report_id": report_id, "file_type": file_type})
