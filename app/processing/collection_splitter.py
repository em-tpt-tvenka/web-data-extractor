

from __future__ import annotations

import hashlib
from typing import Any

from app.platforms.categories import RECORD_TYPE_LISTING
from app.schemas.record import SocialMentionRecord

# Placeholder ids some mappers emit when the page had no product id. They are
# not unique, so a content-derived id is used instead (see _product_id).
_PLACEHOLDER_IDS = {"", "product", "none", "null"}

# Embedded-review keys that are mapped to normalized review fields; every other
# key is preserved verbatim under ``platform_attributes``.
_EMBEDDED_REVIEW_KEYS = {
    "review_id", "reviewer_name", "rating", "title", "review_text", "verified_purchase",
    "posted_date", "posted_relative", "helpful_count", "images", "videos", "language",
    "reviewer_id_hash", "reviewer_location", "country_code", "review_type", "reviewer_type",
}


def _hash(*parts: Any) -> str:
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def _first(*values):
    return next((v for v in values if v not in (None, "", [], {})), None)


def _amount(money: dict | None):
    return (money or {}).get("amount")


def doc_id(*parts: str) -> str:
    """Readable, platform-scoped document id, e.g. ``amazon:B0ABC12345``."""
    return ":".join(str(p) for p in parts)


def _product_entity(d: dict) -> dict:
    products = (d.get("entities") or {}).get("products") or []
    return products[0] if products and isinstance(products[0], dict) else {}


def _product_id(d: dict) -> str:
    """The platform's own product id; for listings that is the source item id,
    for reviews the parent item id. Placeholders get a stable content-derived id."""
    src = d.get("source") or {}
    pe = _product_entity(d)
    is_listing = d.get("record_type") == RECORD_TYPE_LISTING
    pid = _first(pe.get("product_id"),
                 src.get("source_item_id") if is_listing else src.get("parent_item_id"))
    if pid is None or str(pid).strip().lower() in _PLACEHOLDER_IDS:
        basis = _first(pe.get("product_url"), src.get("source_url") if is_listing else None,
                       pe.get("product_name"))
        pid = "gen:" + _hash(basis)[:32]
    return str(pid)


def _media(content: dict, kind: str) -> list[str]:
    media = content.get("media") or []
    if not isinstance(media, list):
        return []
    return [m.get("url") for m in media
            if isinstance(m, dict) and m.get("media_type") == kind and m.get("url")]


def _common(d: dict, product_id: str) -> dict:
    src, tenant, prov = d.get("source") or {}, d.get("tenant") or {}, d.get("provenance") or {}
    return {
        "platform": src.get("platform"),
        "product_id": product_id,
        "tenant_id": tenant.get("tenant_id"),
        "project_id": tenant.get("project_id"),
        "marketplace": src.get("marketplace"),
        "last_collection_run_id": prov.get("collection_run_id"),
        "last_scraped_at": src.get("collected_at"),
    }


# ── products ────────────────────────────────────────────────────────────────

def _product_doc(d: dict, product_id: str) -> dict:
    src, content = d.get("source") or {}, d.get("content") or {}
    pe, attrs = _product_entity(d), d.get("product_attributes") or {}
    tax, metrics = d.get("taxonomy") or {}, d.get("metrics") or {}
    is_listing = d.get("record_type") == RECORD_TYPE_LISTING
    brands = [b.get("brand_name") for b in (d.get("entities") or {}).get("brands") or [] if b.get("brand_name")]
    rating = metrics.get("rating") or {}
    return {
        "_id": doc_id(src.get("platform"), product_id),
        **_common(d, product_id),
        "title": _first(pe.get("product_name"), content.get("title") if is_listing else None),
        "brand": brands[0] if brands else None,
        "brands": brands,
        "sku": pe.get("sku"),
        "model_number": _first(attrs.get("model_number"), pe.get("model_number")),
        "product_url": _first(pe.get("product_url"), src.get("source_url") if is_listing else None),
        "country_code": src.get("country_code"),
        # Product-level text and media only exist on listing records; review
        # records carry the review's own text and media instead.
        "description": content.get("text") if is_listing else None,
        "keywords": content.get("keywords") if is_listing else [],
        "images": _media(content, "image") if is_listing else [],
        "videos": _media(content, "video") if is_listing else [],
        "industry": tax.get("industry"),
        "category": tax.get("category"),
        "subcategory": tax.get("subcategory"),
        "product_type": tax.get("product_type"),
        "taxonomy": tax or None,
        "color": attrs.get("color"),
        "capacity": attrs.get("capacity"),
        "energy_rating": attrs.get("energy_rating"),
        "highlights": attrs.get("highlights") or [],
        "variants": attrs.get("variants") or [],
        "manufacturer": attrs.get("manufacturer"),
        "specifications": attrs.get("specifications") or {},
        "product_attributes": attrs or None,
        # Aggregate ratings are product-level and only present on listings.
        "overall_rating": _first(metrics.get("overall_rating"), rating.get("value")) if is_listing else None,
        "total_ratings": metrics.get("total_ratings") if is_listing else None,
        "total_reviews": _first(metrics.get("total_reviews"), metrics.get("review_count")) if is_listing else None,
        "rating_distribution": (metrics.get("rating_distribution") or None) if is_listing else None,
        "review_summary": d.get("review_summary"),
        "source_record_ids": [d.get("_id")],
    }


def _merge(base: dict, extra: dict) -> dict:
    """Fill empty fields of ``base`` from ``extra`` (never overwrite data)."""
    for k, v in extra.items():
        if k == "source_record_ids":
            base[k] = list(dict.fromkeys((base.get(k) or []) + (v or [])))
        elif base.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
            base[k] = v
    return base


# ── retailers ───────────────────────────────────────────────────────────────

def _retailer_doc(d: dict, product_id: str) -> dict:
    src, pe = d.get("source") or {}, _product_entity(d)
    ent = (d.get("entities") or {}).get("retailer") or {}
    com = d.get("commercial") or {}
    price, avail, deliv = com.get("price") or {}, com.get("availability") or {}, com.get("delivery") or {}
    platform = src.get("platform")
    retailer_id = _first(ent.get("retailer_id"), f"RETAILER_{str(platform).upper()}")
    selling = price.get("selling_price") or {}
    is_listing = d.get("record_type") == RECORD_TYPE_LISTING
    extras = {k: v for k, v in ent.items() if k not in ("retailer_id", "retailer_name")}
    return {
        "_id": doc_id(platform, product_id, retailer_id),
        **_common(d, product_id),
        "retailer_id": retailer_id,
        "retailer_name": ent.get("retailer_name"),
        "seller": extras or None,  # e.g. Amazon seller / ships-from, kept as given
        "product_url": _first(pe.get("product_url"), src.get("source_url") if is_listing else None),
        "country_code": src.get("country_code"),
        "current_price": _amount(selling),
        "original_price": _amount(price.get("original_price")),
        "listed_price": _amount(price.get("listed_price")),
        "lowest_price_with_offers": _amount(price.get("lowest_price_with_offers")),
        "discount_amount": price.get("discount_amount"),
        "discount_percentage": price.get("discount_percentage"),
        "currency": _first(selling.get("currency"), (price.get("original_price") or {}).get("currency"),
                           (com.get("price_range") or {}).get("currency")),
        "price_type": price.get("price_type"),
        "price_observed_at": price.get("price_observed_at"),
        "availability_status": avail.get("status"),
        "stock_quantity": avail.get("stock_quantity"),
        "in_stock": {"in_stock": True, "limited_stock": True, "out_of_stock": False}.get(avail.get("status")),
        "shipping": {
            "delivery_charge": _amount(deliv.get("delivery_charge")),
            "delivery_currency": (deliv.get("delivery_charge") or {}).get("currency"),
            "estimated_delivery_date": deliv.get("estimated_delivery_date"),
            "delivery_type": deliv.get("delivery_type"),
        },
        "promotion": com.get("promotion"),
        "payment_options": com.get("payment_options"),
        "warranty": com.get("warranty"),
        "offers_detail": com.get("offers_detail"),
        "price_range": com.get("price_range"),
        "commercial": com or None,  # full block, platform-specific extras included
        "source_record_ids": [d.get("_id")],
    }


# ── reviews ─────────────────────────────────────────────────────────────────

def _review_doc_from_record(d: dict, product_id: str) -> dict:
    """A review that is its own record (Alibaba, Samsung, Walmart, Best Buy)."""
    src, content, author = d.get("source") or {}, d.get("content") or {}, d.get("author") or {}
    metrics, loc = d.get("metrics") or {}, d.get("location") or {}
    review_id = str(src.get("source_item_id"))
    known = {"_id", "schema_version", "record_type", "tenant", "source", "content", "entities", "taxonomy",
             "product_attributes", "commercial", "metrics", "author", "location", "campaign",
             "deduplication", "provenance", "quality", "lifecycle", "review_summary", "reviews"}
    return {
        "_id": doc_id(src.get("platform"), product_id, review_id),
        **_common(d, product_id),
        "review_id": review_id,
        "review_type": src.get("source_type") or "review",
        "product_name": _product_entity(d).get("product_name"),
        "product_url": _first(_product_entity(d).get("product_url"), src.get("source_url")),
        "reviewer_name": author.get("display_name"),
        "reviewer_id_hash": author.get("source_author_id_hash"),
        "reviewer_type": author.get("author_type"),
        "reviewer_location": _first(author.get("location"), loc.get("city"), loc.get("region")),
        "country_code": _first(loc.get("country_code"), src.get("country_code")),
        "title": content.get("title"),
        "review_text": content.get("text"),
        "language": content.get("language"),
        "rating": (metrics.get("rating") or {}).get("value"),
        "review_date": src.get("published_at"),
        "review_date_text": None,
        "verified_purchase": author.get("is_verified_purchase"),
        "helpful_votes": metrics.get("helpful_votes"),
        "images": _media(content, "image"),
        "videos": _media(content, "video"),
        "content_hash": (d.get("deduplication") or {}).get("content_hash"),
        "source_record_id": d.get("_id"),
        "platform_attributes": {k: v for k, v in d.items() if k not in known} or None,
    }


def _review_doc_from_embedded(d: dict, product_id: str, r: dict) -> dict:
    """A review embedded in a listing's ``reviews`` list (Flipkart, Amazon,
    Alibaba). ``review_type`` / ``reviewer_type`` default to a customer review;
    Alibaba sets them to tell supplier (store) feedback apart."""
    src, platform = d.get("source") or {}, (d.get("source") or {}).get("platform")
    review_id = r.get("review_id") or "gen:" + _hash(
        platform, product_id, r.get("reviewer_name"), r.get("posted_date") or r.get("posted_relative"),
        r.get("rating"), r.get("title"), r.get("review_text"))
    return {
        "_id": doc_id(platform, product_id, review_id),
        **_common(d, product_id),
        "review_id": str(review_id),
        "review_type": r.get("review_type") or "review",
        "product_name": _product_entity(d).get("product_name"),
        "product_url": _first(_product_entity(d).get("product_url"), src.get("source_url")),
        "reviewer_name": r.get("reviewer_name"),
        "reviewer_id_hash": _first(r.get("reviewer_id_hash"),
                                   _hash(r["reviewer_name"]) if r.get("reviewer_name") else None),
        "reviewer_type": r.get("reviewer_type") or "customer",
        "reviewer_location": r.get("reviewer_location"),
        "country_code": _first(r.get("country_code"), src.get("country_code")),
        "title": r.get("title"),
        "review_text": r.get("review_text"),
        "language": r.get("language"),
        "rating": r.get("rating"),
        "review_date": r.get("posted_date"),
        "review_date_text": r.get("posted_relative"),
        "verified_purchase": r.get("verified_purchase"),
        "helpful_votes": r.get("helpful_count"),
        "images": r.get("images") or [],
        "videos": r.get("videos") or [],
        "content_hash": None,
        "source_record_id": d.get("_id"),
        "platform_attributes": {k: v for k, v in r.items() if k not in _EMBEDDED_REVIEW_KEYS} or None,
    }


# ── entry point ─────────────────────────────────────────────────────────────

def split_records(records: list[SocialMentionRecord | dict]) -> dict[str, list[dict]]:
    """Return ``{"products": [...], "retailers": [...], "reviews": [...]}``,
    each de-duplicated by ``_id`` within the batch. Rejected records (missing
    required identity fields) are skipped."""
    products: dict[str, dict] = {}
    retailers: dict[str, dict] = {}
    reviews: dict[str, dict] = {}

    # Listings first, so product/retailer documents prefer listing data and are
    # only gap-filled from review records.
    dicts = [r.to_mongo() if isinstance(r, SocialMentionRecord) else r for r in records]
    dicts.sort(key=lambda d: d.get("record_type") != RECORD_TYPE_LISTING)

    for d in dicts:
        if (d.get("quality") or {}).get("status") == "rejected":
            continue
        if not (d.get("source") or {}).get("platform"):
            continue
        pid = _product_id(d)

        p = _product_doc(d, pid)
        products[p["_id"]] = _merge(products[p["_id"]], p) if p["_id"] in products else p

        rt = _retailer_doc(d, pid)
        retailers[rt["_id"]] = _merge(retailers[rt["_id"]], rt) if rt["_id"] in retailers else rt

        if d.get("record_type") == RECORD_TYPE_LISTING:
            for r in d.get("reviews") or []:
                if isinstance(r, dict):
                    rv = _review_doc_from_embedded(d, pid, r)
                    reviews.setdefault(rv["_id"], rv)
        else:
            rv = _review_doc_from_record(d, pid)
            reviews.setdefault(rv["_id"], rv)

    return {"products": list(products.values()), "retailers": list(retailers.values()),
            "reviews": list(reviews.values())}
