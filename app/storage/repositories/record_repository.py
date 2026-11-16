from __future__ import annotations

from pymongo import UpdateOne

from app.schemas.record import SocialMentionRecord
from app.storage.database import get_db

class RecordRepository:
    @property
    def _col(self):
        return get_db().records

    async def existing_ids_by_source(
        self, tenant_id: str, platform: str, source_item_ids: list[str]
    ) -> dict[str, str]:
        ids = [s for s in set(source_item_ids) if s]
        if not ids:
            return {}
        cursor = self._col.find(
            {
                "tenant.tenant_id": tenant_id,
                "source.platform": platform,
                "source.source_item_id": {"$in": ids},
            },
            {"_id": 1, "source.source_item_id": 1},
        )
        out: dict[str, str] = {}
        async for doc in cursor:
            out[doc["source"]["source_item_id"]] = doc["_id"]
        return out

    async def upsert_many(self, records: list[SocialMentionRecord]) -> int:
        if not records:
            return 0
        ops = []
        for record in records:
            doc = record.to_mongo()
            record_id = doc.pop("_id")
            lifecycle = doc.pop("lifecycle")
            set_fields = {
                **doc,
                "lifecycle.last_seen_at": lifecycle["last_seen_at"],
                "lifecycle.record_status": lifecycle["record_status"],
            }
            ops.append(
                UpdateOne(
                    {"_id": record_id},
                    {
                        "$set": set_fields,
                        "$setOnInsert": {"lifecycle.first_seen_at": lifecycle["first_seen_at"]},
                    },
                    upsert=True,
                )
            )
        result = await self._col.bulk_write(ops, ordered=False)
        return (result.upserted_count or 0) + (result.modified_count or 0)
