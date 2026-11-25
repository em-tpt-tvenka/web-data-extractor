from __future__ import annotations

import hashlib
import re
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
        ([("brand", ASCENDING)], {"name": "brand"}),
        ([("category", ASCENDING)], {"name": "category"}),
        ([("tenant_id", ASCENDING)], {"name": "tenant"}),
    ],
    # Legacy retailer structure (one doc per product+retailer): still used by every platform that is
    # NOT in _SELLER_PLATFORMS. The unique index is partial so seller documents (which have no
    # product_id) never collide on null.
    RETAILERS: [
        ([("platform", ASCENDING), ("product_id", ASCENDING), ("retailer_id", ASCENDING)],
         {"unique": True, "name": "uq_platform_product_retailer",
          "partialFilterExpression": {"product_id": {"$exists": True}}}),
        ([("retailer_id", ASCENDING)], {"name": "retailer"}),
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


# One document per marketplace seller (retailer). Only docs with a string seller_name are indexed here.
_SELLER_INDEX = (
    [("platform", ASCENDING), ("marketplace_retailer_id", ASCENDING)],
    {"unique": True, "name": "uq_platform_seller",
     "partialFilterExpression": {"seller_name": {"$type": "string"}}},
)
_LEGACY_RETAILER_INDEX = "uq_platform_product_retailer"
SELLER_OTHER_KEY = "seller_other_details"
# Platforms whose retailers are saved as seller documents (save_seller_retailers). Every other
# platform keeps the legacy retailer save. Add a platform here when its adapter starts attaching
# `seller_details` to its records.
_SELLER_PLATFORMS = {"flipkart", "amazon"}
_MARKETPLACE_OWNED = {"flipkart", "retailnet", "amazon", "amazon.in", "amazon.com"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _g(obj, *names):
    """Safe nested get that works for dicts and pydantic/objects."""
    for n in names:
        if obj is None:
            return None
        obj = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
    return obj


def _seller_view(rec) -> dict | None:
    """Pull the retailer-relevant bits out of one normalized record (object or dict)."""
    seller = _g(rec, "seller_details")
    name = re.sub(r"\s+", " ", (_g(seller, "name") or "")).strip() if seller else ""
    if not name:
        return None
    # Amazon sends retailer_key = seller id (stable, unique); Flipkart has none, so the name is used.
    key = str(_g(seller, "retailer_key") or name)
    slug = re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
    if not slug:
        return None
    platform = _g(rec, "source", "platform")
    if not platform:
        return None
    rid = f"RETAILER_{slug}"
    return {
        "_id": "retailer_" + hashlib.sha256(f"{platform}|{rid}".encode()).hexdigest()[:16],
        "platform": platform,
        "marketplace": _g(rec, "source", "marketplace"),
        "country_code": _g(rec, "source", "country_code"),
        "retailer_name": name,
        "marketplace_retailer_id": rid,
        "marketplace_retailer_name": name,
        "seller_name": name,
        "seller_rating": _g(seller, "rating"),
        "seller_type": "marketplace_owned" if name.lower() in _MARKETPLACE_OWNED else "third_party",
        "seller_profile_url": _g(seller, "profile_url"),
        SELLER_OTHER_KEY: dict(_g(seller, "other_details") or {}),
        "product_ids": [x for x in [_g(rec, "_id") or _g(rec, "id")] if x],
        "product_urls": [x for x in [_g(rec, "source", "source_url")] if x],
    }


def _merge_sellers(docs: list[dict]) -> list[dict]:
    """Collapse several records of the same seller (same run) into one doc."""
    merged: dict[str, dict] = {}
    for d in docs:
        cur = merged.get(d["_id"])
        if cur is None:
            merged[d["_id"]] = d
            continue
        for k in ("product_ids", "product_urls"):
            cur[k] = list(dict.fromkeys(cur[k] + d[k]))
        cur[SELLER_OTHER_KEY].update({k: v for k, v in d[SELLER_OTHER_KEY].items() if v})
        for k in ("seller_rating", "seller_profile_url"):
            cur[k] = d[k] if d[k] is not None else cur[k]
    return list(merged.values())


def _seller_upsert_op(doc: dict, now: str) -> UpdateOne:
    """Create the retailer on first sight; otherwise refresh it and ADD the product (never replace)."""
    set_fields = {
        "retailer_name": doc["retailer_name"],
        "marketplace_retailer_name": doc["marketplace_retailer_name"],
        "seller_name": doc["seller_name"],
        "seller_type": doc["seller_type"],
        "marketplace": doc["marketplace"],
        "country_code": doc["country_code"],
        "lifecycle.last_seen_at": now,
        "lifecycle.record_status": "active",
    }
    nullable = ("seller_rating", "seller_profile_url")
    for k in nullable:
        if doc[k] is not None:
            set_fields[k] = doc[k]
    details = {k: v for k, v in doc[SELLER_OTHER_KEY].items() if v}
    for k, v in details.items():
        set_fields[f"{SELLER_OTHER_KEY}.{k}"] = v  # dotted: keeps sections we didn't re-scrape

    on_insert = {
        "_id": doc["_id"],
        "lifecycle.first_seen_at": now,
    }
    on_insert.update({k: None for k in nullable if doc[k] is None})
    if not details:
        on_insert[SELLER_OTHER_KEY] = {}

    # Identity = platform + retailer id (NOT _id / tenant): an existing retailer is always found and
    # updated in place, whichever job or tenant scraped it. Nothing is ever deleted or replaced.
    return UpdateOne(
        {"platform": doc["platform"], "marketplace_retailer_id": doc["marketplace_retailer_id"]},
        {
            "$set": set_fields,
            "$setOnInsert": on_insert,
            "$unset": {"tenant": ""},  # retailers carry no tenant_id / project_id (also cleans older docs)
            "$addToSet": {
                "product_ids": {"$each": doc["product_ids"]},
                "product_urls": {"$each": doc["product_urls"]},
            },
        },
        upsert=True,
    )


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
        await self._migrate_legacy_retailer_index()
        for name, specs in _INDEXES.items():
            for keys, opts in specs:
                await db[name].create_index(keys, **opts)
        try:
            await db[RETAILERS].create_index(_SELLER_INDEX[0], **_SELLER_INDEX[1])
        except PyMongoError as exc:
            _log.error("event=seller_index_failed error=%s", exc)
        self._indexed_dbs.add(key)

    async def _migrate_legacy_retailer_index(self) -> None:
        """Older deployments created uq_platform_product_retailer without a partial filter, which makes
        every seller document (no product_id) collide on null. Drop it once; it is re-created as partial."""
        coll = self._db[RETAILERS]
        try:
            info = await coll.index_information()
            if "uq_platform_tenant_seller" in info:
                await coll.drop_index("uq_platform_tenant_seller")
            legacy = info.get(_LEGACY_RETAILER_INDEX)
            if legacy and not legacy.get("partialFilterExpression"):
                await coll.drop_index(_LEGACY_RETAILER_INDEX)
                _log.info("event=legacy_retailer_index_dropped name=%s", _LEGACY_RETAILER_INDEX)
        except PyMongoError as exc:
            _log.warning("event=legacy_retailer_index_check_failed error=%s", exc)

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

    async def save_seller_retailers(self, records: list[SocialMentionRecord | dict]) -> dict:
        """Upsert one retailer doc per scraped seller. If the seller already exists the product id /
        url are added to it and its details refreshed; otherwise a new retailer doc is created."""
        views = []
        for rec in records:
            try:
                v = _seller_view(rec)
            except Exception as exc:  # one malformed record must not stop the others
                _log.error("event=seller_view_failed error=%s", exc)
                continue
            if v:
                views.append(v)
        docs = _merge_sellers(views)
        if not docs:
            return {"inserted": 0, "updated": 0, "errors": 0}
        now = _now()
        try:
            res = await self._db[RETAILERS].bulk_write([_seller_upsert_op(d, now) for d in docs], ordered=False)
            return {"inserted": res.upserted_count or 0, "updated": res.matched_count or 0, "errors": 0}
        except BulkWriteError as exc:
            details = exc.details or {}
            errors = details.get("writeErrors") or []
            _log.error("event=seller_bulk_partial errors=%d first=%s",
                       len(errors), (errors[0].get("errmsg") if errors else None))
            return {"inserted": details.get("nUpserted", 0), "updated": details.get("nMatched", 0),
                    "errors": len(errors)}

    async def save_scraped_data(self, records: list[SocialMentionRecord | dict]) -> dict:
        """Split normalized records and upsert all three collections.
        Returns per-collection counts; raises only on connection-level errors."""
        await self.ensure_indexes()
        split = split_records(records)
        # Flipkart / Amazon retailers are saved as seller documents below; every other platform
        # keeps the legacy retailer docs produced by the splitter.
        legacy_recs = [x for x in records if _g(x, "source", "platform") not in _SELLER_PLATFORMS]
        if len(legacy_recs) == len(records):
            legacy_retailers = split["retailers"]
        else:
            legacy_retailers = split_records(legacy_recs)["retailers"] if legacy_recs else []
        result = {
            PRODUCTS: await self.save_product(split["products"]),
            RETAILERS: await self.save_retailer(legacy_retailers),  # legacy structure, other platforms only
            REVIEWS: await self.save_reviews(split["reviews"]),
        }
        _log.info("event=collections_saved products=%s retailers=%s reviews=%s",
                  result[PRODUCTS], result[RETAILERS], result[REVIEWS])
        try:
            result["seller_retailers"] = await self.save_seller_retailers(records)
            _log.info("event=seller_retailers_saved %s", result["seller_retailers"])
        except Exception as exc:  # never let seller storage affect product/review saving
            _log.error("event=seller_retailers_failed error=%s", exc)
            result["seller_retailers"] = {"inserted": 0, "updated": 0, "errors": 1}
        return result


__all__ = ["CollectionsRepository", "PRODUCTS", "REVIEWS", "RETAILERS", "PyMongoError"]