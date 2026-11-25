

from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ASCENDING, UpdateOne
from pymongo.errors import BulkWriteError, PyMongoError

from app.core.logging import get_logger
from app.processing.collection_splitter import split_records
from app.schemas.record import SocialMentionRecord
from app.storage.database import get_db

_log = get_logger("collections_repository")

PRODUCTS, REVIEWS, RETAILERS = "products", "reviews", "retailers"

_INDEXES = {
    PRODUCTS: [
        ([("platform", ASCENDING), ("product_id", ASCENDING)], {"unique": True, "name": "uq_platform_product"}),
        ([("retailer_id", ASCENDING)], {"name": "retailer"}),
        ([("brand", ASCENDING)], {"name": "brand"}),
        ([("category", ASCENDING)], {"name": "category"}),
        ([("tenant_id", ASCENDING)], {"name": "tenant"}),
    ],
    RETAILERS: [
        ([("platform", ASCENDING), ("retailer_id", ASCENDING)],
         {"unique": True, "name": "uq_platform_retailer"}),
        ([("tenant_id", ASCENDING)], {"name": "tenant"}),
    ],
    REVIEWS: [
        ([("platform", ASCENDING), ("product_id", ASCENDING), ("review_id", ASCENDING)],
         {"unique": True, "name": "uq_platform_product_review"}),
        ([("platform", ASCENDING), ("product_id", ASCENDING), ("review_date", ASCENDING)],
         {"name": "product_review_date"}),
        ([("tenant_id", ASCENDING)], {"name": "tenant"}),
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_op(doc: dict, now: str) -> UpdateOne:
    doc = dict(doc)
    _id = doc.pop("_id")
    record_ids = [r for r in (doc.pop("source_record_ids", None) or [doc.get("source_record_id")]) if r]
    set_fields = {k: v for k, v in doc.items() if v is not None}
    set_fields["last_seen_at"] = now
    on_insert = {k: None for k, v in doc.items() if v is None}
    on_insert["first_seen_at"] = now
    update = {"$set": set_fields, "$setOnInsert": on_insert}
    if record_ids:
        update["$addToSet"] = {"source_record_ids": {"$each": record_ids}}
    return UpdateOne({"_id": _id}, update, upsert=True)


class CollectionsRepository:
    """save_product / save_reviews / save_retailer / save_scraped_data."""

    _indexed_dbs: set[int] = set()

    @property
    def _db(self):
        return get_db()

    async def ensure_indexes(self) -> None:
        db = self._db
        key = id(db.client)
        if key in self._indexed_dbs:
            return
        for name, specs in _INDEXES.items():
            for keys, opts in specs:
                await db[name].create_index(keys, **opts)
        self._indexed_dbs.add(key)

    async def _bulk_upsert(self, collection: str, docs: list[dict]) -> dict:
        if not docs:
            return {"inserted": 0, "updated": 0, "errors": 0}
        now = _now()
        try:
            res = await self._db[collection].bulk_write([_upsert_op(d, now) for d in docs], ordered=False)
            return {"inserted": res.upserted_count or 0, "updated": res.matched_count or 0, "errors": 0}
        except BulkWriteError as exc:
            details = exc.details or {}
            errors = details.get("writeErrors") or []
            _log.error("event=collection_bulk_partial collection=%s errors=%d first=%s",
                       collection, len(errors), (errors[0].get("errmsg") if errors else None))
            return {"inserted": details.get("nUpserted", 0), "updated": details.get("nMatched", 0),
                    "errors": len(errors)}

    async def save_product(self, products: dict | list[dict]) -> dict:
        return await self._bulk_upsert(PRODUCTS, products if isinstance(products, list) else [products])

    async def save_retailer(self, retailers: dict | list[dict]) -> dict:
        return await self._bulk_upsert(RETAILERS, retailers if isinstance(retailers, list) else [retailers])

    async def save_reviews(self, reviews: list[dict]) -> dict:
        return await self._bulk_upsert(REVIEWS, reviews)

    async def save_scraped_data(self, records: list[SocialMentionRecord | dict]) -> dict:
        """Split normalized records and upsert all three collections.
        Returns per-collection counts; raises only on connection-level errors."""
        await self.ensure_indexes()
        split = split_records(records)
        result = {
            PRODUCTS: await self.save_product(split["products"]),
            RETAILERS: await self.save_retailer(split["retailers"]),
            REVIEWS: await self.save_reviews(split["reviews"]),
        }
        _log.info("event=collections_saved products=%s retailers=%s reviews=%s",
                  result[PRODUCTS], result[RETAILERS], result[REVIEWS])
        return result


__all__ = ["CollectionsRepository", "PRODUCTS", "REVIEWS", "RETAILERS", "PyMongoError"]
