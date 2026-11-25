"""Retrieve and enrich scraped reviews for a set of report filters.

Reads the ``reviews`` collection (read-only) and joins brand/category from the
``products`` collection, then classifies each review locally (sentiment,
aspects, complaints). Returns the enriched list plus a snapshot signature used
for summary idempotency/reuse. The scraped collections are never mutated.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.logging import get_logger
from app.reporting import AGGREGATION_VERSION
from app.reporting.nlp.aspect_extractor import extract_aspects, extract_complaints
from app.reporting.nlp.sentiment_classifier import classify
from app.reporting.schemas.report_request import SummaryFiltersIn
from app.reporting.utils.report_helpers import parse_review_date
from app.storage.database import get_db

_log = get_logger("reporting.data")

_PRODUCT_FIELDS = {
    "_id": 0, "platform": 1, "product_id": 1, "brand": 1, "brands": 1,
    "title": 1, "category": 1, "subcategory": 1, "product_type": 1, "industry": 1,
}


@dataclass
class DataBundle:
    reviews: list[dict] = field(default_factory=list)
    product_count: int = 0
    snapshot_signature: str = ""
    snapshot_meta: dict[str, Any] = field(default_factory=dict)


class ReportDataService:
    @property
    def _db(self):
        return get_db()

    @staticmethod
    def _scope_query(filters: SummaryFiltersIn) -> dict:
        q: dict[str, Any] = {}
        if filters.tenant_id is not None:
            q["tenant_id"] = filters.tenant_id
        if filters.project_id is not None:
            q["project_id"] = filters.project_id
        if filters.platforms:
            q["platform"] = {"$in": list(filters.platforms)}
        return q

    async def _product_map(self, filters: SummaryFiltersIn) -> dict[tuple[str, str], dict]:
        cursor = self._db["products"].find(self._scope_query(filters), _PRODUCT_FIELDS)
        out: dict[tuple[str, str], dict] = {}
        async for p in cursor:
            out[(p.get("platform"), p.get("product_id"))] = p
        return out

    @staticmethod
    def _brand_of(product: dict, review: dict, wanted_brands: set[str]) -> str | None:
        brand = product.get("brand")
        if not brand:
            brands = product.get("brands") or []
            brand = brands[0] if brands else None
        if brand:
            return brand
        # Fallback: match a requested brand name inside the product title / review
        # text. Heuristic, used only when the product has no brand field.
        if wanted_brands:
            hay = f"{product.get('title') or ''} {review.get('review_text') or ''}".lower()
            for b in wanted_brands:
                if b and b.lower() in hay:
                    return b
        return None

    async def fetch(self, filters: SummaryFiltersIn) -> DataBundle:
        products = await self._product_map(filters)
        wanted_brands = {b for b in (filters.brands or [])}
        wanted_brands_lc = {b.lower() for b in wanted_brands}
        wanted_cats_lc = {c.lower() for c in (filters.categories or [])}
        dr = filters.date_range
        start: date | None = dr.start if dr else None
        end: date | None = dr.end if dr else None

        enriched: list[dict] = []
        products_seen: set[tuple[str, str]] = set()
        count = 0
        last_seen_max = ""

        cursor = self._db["reviews"].find(self._scope_query(filters))
        async for rv in cursor:
            count += 1
            ls = rv.get("last_seen_at") or ""
            if isinstance(ls, str) and ls > last_seen_max:
                last_seen_max = ls

            key = (rv.get("platform"), rv.get("product_id"))
            product = products.get(key, {})
            brand = self._brand_of(product, rv, wanted_brands)
            category = product.get("category")

            # Brand filter.
            if wanted_brands_lc and (brand or "").lower() not in wanted_brands_lc:
                continue
            # Category filter.
            if wanted_cats_lc and (category or "").lower() not in wanted_cats_lc:
                continue

            d = parse_review_date(rv.get("review_date"))
            if (start or end) and d is None:
                continue  # dated filter: undated reviews can't be confirmed in range
            if start and d and d < start:
                continue
            if end and d and d > end:
                continue

            rating = rv.get("rating")
            try:
                rating = float(rating) if rating is not None else None
            except (TypeError, ValueError):
                rating = None
            text = rv.get("review_text") or rv.get("title") or ""
            sentiment = classify(rating, text)

            enriched.append({
                "review_id": rv.get("review_id") or rv.get("_id"),
                "source_record_id": rv.get("source_record_id"),
                "platform": rv.get("platform"),
                "product_id": rv.get("product_id"),
                "brand": brand,
                "category": category,
                "subcategory": product.get("subcategory"),
                "product_type": product.get("product_type"),
                "industry": product.get("industry"),
                "product_name": rv.get("product_name") or product.get("title"),
                "rating": rating,
                "title": rv.get("title"),
                "text": text,
                "date": d,
                "country_code": rv.get("country_code"),
                "verified_purchase": rv.get("verified_purchase"),
                "helpful_votes": rv.get("helpful_votes"),
                "language": rv.get("language"),
                "sentiment": sentiment,
                "aspects": extract_aspects(text, sentiment),
                "complaints": extract_complaints(text, sentiment),
            })
            products_seen.add(key)

        sig_src = (
            f"{filters.model_dump_json()}|{AGGREGATION_VERSION}|"
            f"n={len(enriched)}|scan={count}|ls={last_seen_max}"
        )
        signature = "snap:" + hashlib.sha256(sig_src.encode()).hexdigest()[:24]
        _log.info(
            "event=data_fetched scanned=%d matched=%d products=%d",
            count, len(enriched), len(products_seen),
        )
        return DataBundle(
            reviews=enriched,
            product_count=len(products_seen),
            snapshot_signature=signature,
            snapshot_meta={"scanned_reviews": count, "matched_reviews": len(enriched),
                           "max_last_seen_at": last_seen_max},
        )
