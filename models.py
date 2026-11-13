from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import os
from typing import List, Optional, Dict, Any, Tuple

from config import (
    PLATFORM_CONFIGS,
    OUTPUT_JSON_PATH,
    OUTPUT_REVIEWS_JSON_PATH,
    OUTPUT_LAPTOPS_JSON_PATH,
    OUTPUT_LAPTOPS_REVIEWS_JSON_PATH,
)

SCHEMA_VERSION = "2.0.0"


def now_iso_utc() -> str:
    """Returns current UTC timestamp in ISO-8601 Z format (e.g. 2026-11-11T15:29:00Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_dt_human_readable(dt: datetime) -> str:
    """Formats a datetime in local time (e.g. 'November 12, 2026, 12:55:16 PM IST')."""
    local_dt = dt.astimezone() if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc).astimezone()
    tz_name = local_dt.tzname() or ""
    if " " in tz_name:
        tz_abbr = "".join(w[0].upper() for w in tz_name.split() if w)
    else:
        tz_abbr = tz_name
    if not tz_abbr:
        offset = local_dt.strftime("%z")
        tz_abbr = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "Local"
    base = local_dt.strftime(f"%B {local_dt.day}, %Y, %I:%M:%S %p")
    return f"{base} {tz_abbr}".strip()


def now_human_readable() -> str:
    """Returns current local timestamp in human-readable format (e.g. 'November 12, 2026, 12:55:16 PM IST')."""
    return _format_dt_human_readable(datetime.now().astimezone())


def format_scraped_at_human_readable(ts: Optional[str]) -> Optional[str]:
    """Converts an ISO-8601 timestamp string into local human-readable format when parseable."""
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw:
        return None
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
            return d.strftime(f"%B {d.day}, %Y")
        except ValueError:
            return raw
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return _format_dt_human_readable(dt)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M:%S %p"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return _format_dt_human_readable(dt)
        except ValueError:
            continue
    return raw


def normalize_metadata_scraped_at(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures doc['metadata']['scraped_at'] is formatted as a human-readable timestamp."""
    meta = doc.get("metadata")
    if isinstance(meta, dict):
        ts = meta.get("scraped_at")
        meta["scraped_at"] = format_scraped_at_human_readable(ts) or now_human_readable()
    return doc


def normalize_iso_utc(ts: Optional[str]) -> Optional[str]:
    """Normalizes timestamp strings to ISO-8601 UTC Z format when parseable."""
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw:
        return None
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    try:
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return raw


def get_reviews_json_path(products_filepath: str) -> str:
    """Resolves the paired Schema v2.0.0 review output path for a product JSON path."""
    abs_in = os.path.abspath(products_filepath).lower()
    if abs_in == os.path.abspath(OUTPUT_JSON_PATH).lower():
        return os.path.join(os.path.dirname(products_filepath) or ".", os.path.basename(OUTPUT_REVIEWS_JSON_PATH))
    if abs_in == os.path.abspath(OUTPUT_LAPTOPS_JSON_PATH).lower():
        return os.path.join(os.path.dirname(products_filepath) or ".", os.path.basename(OUTPUT_LAPTOPS_REVIEWS_JSON_PATH))
    base, ext = os.path.splitext(products_filepath)
    if not ext:
        ext = ".json"
    if base.endswith("_reviews"):
        return f"{base}{ext}"
    return f"{base}_reviews{ext}"


def get_platform_metadata(platform: str) -> Dict[str, str]:
    """Looks up marketplace_type, region, and default currency from PLATFORM_CONFIGS."""
    key = (platform or "generic").strip().lower()
    cfg = PLATFORM_CONFIGS.get(key, PLATFORM_CONFIGS.get("generic", {}))
    return {
        "platform": key,
        "marketplace_type": cfg.get("marketplace_type", "B2C"),
        "region": cfg.get("region", "GLOBAL"),
        "currency": cfg.get("currency", "USD"),
    }


def is_v2_product_doc(doc: Optional[Dict[str, Any]]) -> bool:
    """Returns True if dict follows Schema v2.0.0 Product Details layout."""
    return isinstance(doc, dict) and "metadata" in doc and "product" in doc


def is_v2_review_doc(doc: Optional[Dict[str, Any]]) -> bool:
    """Returns True if dict follows Schema v2.0.0 Review Details layout."""
    return isinstance(doc, dict) and "metadata" in doc and "reviews" in doc


def _normalize_star_distribution(raw_dist: Optional[Dict[Any, Any]]) -> Dict[str, int]:
    """Normalizes star distribution to canonical {'5': int, '4': int, '3': int, '2': int, '1': int}."""
    src = raw_dist if isinstance(raw_dist, dict) else {}

    def _pick(star: str, alt: str) -> int:
        val = src.get(star)
        if val is None:
            val = src.get(int(star))
        if val is None:
            val = src.get(alt)
        try:
            return int(val) if val is not None else 0
        except (TypeError, ValueError):
            return 0

    return {
        "5": _pick("5", "5_star"),
        "4": _pick("4", "4_star"),
        "3": _pick("3", "3_star"),
        "2": _pick("2", "2_star"),
        "1": _pick("1", "1_star"),
    }


def _to_float_or_none(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def build_product_v2(
    *,
    platform: str,
    platform_product_id: Optional[str],
    product_url: str,
    title: str,
    brand: Optional[str] = None,
    manufacturer: Optional[str] = None,
    category_path: Optional[List[str]] = None,
    condition: Optional[str] = "new",
    introduced_date: Optional[str] = None,
    parent_product_id: Optional[str] = None,
    sku: Optional[str] = None,
    upc_ean: Optional[str] = None,
    model_number: Optional[str] = None,
    currency: Optional[str] = None,
    list_price: Optional[float] = None,
    current_price: Optional[float] = None,
    discount_percentage: Optional[float] = None,
    is_on_sale: Optional[bool] = None,
    b2b_price_tiers: Optional[List[Dict[str, Any]]] = None,
    moq: Optional[int] = None,
    in_stock: bool = False,
    stock_status_text: Optional[str] = None,
    is_platform_fulfilled: Optional[bool] = None,
    free_shipping: Optional[bool] = None,
    estimated_delivery_days: Optional[int] = None,
    no_cost_emi_available: bool = False,
    emi_starting_per_month: Optional[float] = None,
    no_cost_emi_offers: Optional[List[str]] = None,
    short_description: Optional[str] = None,
    bullet_features: Optional[List[str]] = None,
    specifications: Optional[Dict[str, Any]] = None,
    included_items: Optional[List[str]] = None,
    variants: Optional[List[Dict[str, Any]]] = None,
    oem_odm_available: Optional[bool] = None,
    custom_logo: Optional[bool] = None,
    custom_packaging: Optional[bool] = None,
    images: Optional[List[Dict[str, Any]]] = None,
    videos: Optional[List[Dict[str, Any]]] = None,
    seller_id: Optional[str] = None,
    seller_name: Optional[str] = None,
    store_url: Optional[str] = None,
    is_official_brand_store: Optional[bool] = None,
    is_verified_seller: Optional[bool] = None,
    years_active: Optional[int] = None,
    seller_rating: Optional[float] = None,
    on_time_delivery_rate: Optional[float] = None,
    response_rate: Optional[float] = None,
    marketplace_type: Optional[str] = None,
    region: Optional[str] = None,
    scraped_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Constructs a Schema v2.0.0 Product Details document."""
    p_meta = get_platform_metadata(platform)
    resolved_currency = (currency or p_meta["currency"] or "USD").upper()
    lp = _to_float_or_none(list_price)
    cp = _to_float_or_none(current_price)

    computed_discount = _to_float_or_none(discount_percentage)
    if computed_discount is None and lp and cp and lp > cp > 0:
        computed_discount = round(((lp - cp) / lp) * 100.0, 2)

    if is_on_sale is None:
        is_on_sale = bool((lp and cp and lp > cp) or (computed_discount and computed_discount > 0))

    resolved_moq = _to_int_or_none(moq)
    if resolved_moq is None and p_meta["marketplace_type"] in ("B2C", "D2C"):
        resolved_moq = 1

    clean_cat_path = [str(c).strip() for c in (category_path or []) if str(c).strip()]

    return {
        "metadata": {
            "platform": p_meta["platform"],
            "marketplace_type": marketplace_type or p_meta["marketplace_type"],
            "region": region or p_meta["region"],
            "scraped_at": format_scraped_at_human_readable(scraped_at) or now_human_readable(),
            "schema_version": SCHEMA_VERSION,
        },
        "product": {
            "identifiers": {
                "platform_product_id": platform_product_id,
                "parent_product_id": parent_product_id,
                "sku": sku,
                "upc_ean": upc_ean,
                "model_number": model_number,
                "product_url": product_url,
            },
            "core": {
                "title": title,
                "brand": brand,
                "manufacturer": manufacturer,
                "category_path": clean_cat_path,
                "condition": condition,
                "introduced_date": introduced_date,
            },
            "pricing_and_availability": {
                "currency": resolved_currency,
                "list_price": lp,
                "current_price": cp,
                "discount_percentage": computed_discount,
                "is_on_sale": bool(is_on_sale),
                "b2b_price_tiers": b2b_price_tiers or [],
                "moq": resolved_moq,
                "in_stock": bool(in_stock),
                "stock_status_text": stock_status_text,
                "fulfillment": {
                    "is_platform_fulfilled": is_platform_fulfilled,
                    "free_shipping": free_shipping,
                    "estimated_delivery_days": _to_int_or_none(estimated_delivery_days),
                },
                "financing": {
                    "no_cost_emi_available": bool(no_cost_emi_available),
                    "emi_starting_per_month": _to_float_or_none(emi_starting_per_month),
                    "no_cost_emi_offers": [str(o).strip() for o in (no_cost_emi_offers or []) if str(o).strip()],
                },
            },
            "content_and_specs": {
                "short_description": short_description,
                "bullet_features": bullet_features or [],
                "specifications": specifications or {},
                "included_items": included_items or [],
                "variants": variants or [],
                "b2b_customization": {
                    "oem_odm_available": oem_odm_available,
                    "custom_logo": custom_logo,
                    "custom_packaging": custom_packaging,
                },
            },
            "media": {
                "images": images or [],
                "videos": videos or [],
            },
            "seller": {
                "seller_id": seller_id,
                "seller_name": seller_name,
                "store_url": store_url,
                "is_official_brand_store": is_official_brand_store,
                "is_verified_seller": is_verified_seller,
                "years_active": _to_int_or_none(years_active),
                "seller_rating": _to_float_or_none(seller_rating),
                "on_time_delivery_rate": _to_float_or_none(on_time_delivery_rate),
                "response_rate": _to_float_or_none(response_rate),
            },
        },
    }


def build_review_v2(
    *,
    platform: str,
    platform_product_id: Optional[str],
    parent_product_id: Optional[str] = None,
    seller_id: Optional[str] = None,
    average_rating: Optional[float] = None,
    total_ratings: int = 0,
    total_written_reviews: int = 0,
    recommended_percentage: Optional[float] = None,
    star_distribution: Optional[Dict[Any, Any]] = None,
    aspect_ratings: Optional[Dict[str, float]] = None,
    product_review_items: Optional[List[Dict[str, Any]]] = None,
    store_summary: Optional[Dict[str, Any]] = None,
    store_review_items: Optional[List[Dict[str, Any]]] = None,
    scraped_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Constructs a Schema v2.0.0 Review Details document."""
    p_meta = get_platform_metadata(platform)
    norm_dist = _normalize_star_distribution(star_distribution)

    items = product_review_items or []
    written_count = int(total_written_reviews or 0)
    if written_count == 0 and items:
        written_count = sum(1 for it in items if (it.get("body") or "").strip())

    dist_sum = sum(norm_dist.values())
    ratings_total = int(total_ratings or 0)
    if ratings_total == 0:
        ratings_total = max(dist_sum, len(items))

    st_sum = store_summary if isinstance(store_summary, dict) else {}
    sub_scores = st_sum.get("sub_scores") if isinstance(st_sum.get("sub_scores"), dict) else {}

    return {
        "metadata": {
            "platform": p_meta["platform"],
            "platform_product_id": platform_product_id,
            "parent_product_id": parent_product_id,
            "seller_id": seller_id,
            "scraped_at": format_scraped_at_human_readable(scraped_at) or now_human_readable(),
            "schema_version": SCHEMA_VERSION,
        },
        "reviews": {
            "product_reviews": {
                "summary": {
                    "platform_product_id": platform_product_id,
                    "average_rating": _to_float_or_none(average_rating),
                    "total_ratings": ratings_total,
                    "total_written_reviews": written_count,
                    "recommended_percentage": _to_float_or_none(recommended_percentage),
                    "star_distribution": norm_dist,
                    "aspect_ratings": aspect_ratings or {},
                },
                "items": items,
            },
            "store_reviews": {
                "summary": {
                    "seller_id": st_sum.get("seller_id", seller_id),
                    "average_store_rating": _to_float_or_none(st_sum.get("average_store_rating")),
                    "total_store_reviews": int(st_sum.get("total_store_reviews") or len(store_review_items or [])),
                    "sub_scores": {
                        "seller_service": _to_float_or_none(sub_scores.get("seller_service")),
                        "shipping_speed": _to_float_or_none(sub_scores.get("shipping_speed")),
                        "item_accuracy": _to_float_or_none(sub_scores.get("item_accuracy")),
                    },
                },
                "items": store_review_items or [],
            },
        },
    }


def make_review_key(doc: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Returns canonical (platform, platform_product_id) lookup key for pairing products & reviews."""
    if not isinstance(doc, dict):
        return ("", "")
    if is_v2_product_doc(doc):
        meta = doc.get("metadata") or {}
        ids = (doc.get("product") or {}).get("identifiers") or {}
        plat = str(meta.get("platform") or "").strip().lower()
        pid = str(ids.get("platform_product_id") or ids.get("product_url") or "").strip()
        return (plat, pid)
    if is_v2_review_doc(doc):
        meta = doc.get("metadata") or {}
        plat = str(meta.get("platform") or "").strip().lower()
        pid = str(meta.get("platform_product_id") or "").strip()
        return (plat, pid)
    plat = str(doc.get("platform") or "samsung").strip().lower()
    pid = str(doc.get("product_id") or doc.get("product_url") or "").strip()
    return (plat, pid)


def extract_flat_summary(
    product_doc: Optional[Dict[str, Any]],
    review_doc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extracts a uniform flat view from either Schema v2.0.0 or legacy v1 documents."""
    doc = product_doc or {}
    if is_v2_product_doc(doc):
        meta = doc.get("metadata") or {}
        prod = doc.get("product") or {}
        ids = prod.get("identifiers") or {}
        core = prod.get("core") or {}
        pa = prod.get("pricing_and_availability") or {}
        fulfill = pa.get("fulfillment") or {}
        specs = prod.get("content_and_specs") or {}
        media = prod.get("media") or {}
        seller = prod.get("seller") or {}

        cat_path = core.get("category_path") or []
        cat_label = cat_path[-1] if isinstance(cat_path, list) and cat_path else ""
        currency = (pa.get("currency") or "USD").upper()
        list_price = pa.get("list_price")
        current_price = pa.get("current_price")
        discount_pct = pa.get("discount_percentage")
        in_stock = bool(pa.get("in_stock", False))
        stock_text = pa.get("stock_status_text")

        r_meta = (review_doc or {}).get("metadata") or {}
        r_block = (review_doc or {}).get("reviews") or doc.get("reviews") or {}
        pr_block = r_block.get("product_reviews") or {}
        pr_sum = pr_block.get("summary") or {}
        pr_items = pr_block.get("items") or []

        avg_rating = pr_sum.get("average_rating")
        total_ratings = int(pr_sum.get("total_ratings") or 0)
        total_written = int(pr_sum.get("total_written_reviews") or 0)
        star_dist = _normalize_star_distribution(pr_sum.get("star_distribution"))
        total_reviews = max(total_ratings, total_written, sum(star_dist.values()))

        return {
            "platform": meta.get("platform") or "generic",
            "marketplace_type": meta.get("marketplace_type") or "B2C",
            "region": meta.get("region") or "GLOBAL",
            "scraped_at": meta.get("scraped_at"),
            "schema_version": meta.get("schema_version") or SCHEMA_VERSION,
            "product_id": ids.get("platform_product_id") or "",
            "platform_product_id": ids.get("platform_product_id") or "",
            "parent_product_id": ids.get("parent_product_id"),
            "sku": ids.get("sku"),
            "upc_ean": ids.get("upc_ean"),
            "model_number": ids.get("model_number"),
            "product_url": ids.get("product_url") or "",
            "product_name": core.get("title") or "",
            "title": core.get("title") or "",
            "brand": core.get("brand") or "",
            "manufacturer": core.get("manufacturer") or "",
            "category": cat_label,
            "category_path": cat_path,
            "condition": core.get("condition"),
            "introduced_date": core.get("introduced_date"),
            "currency": currency,
            "list_price": list_price,
            "current_price": current_price,
            "discount_percentage": discount_pct,
            "is_on_sale": bool(pa.get("is_on_sale", False)),
            "in_stock": in_stock,
            "stock_status_text": stock_text,
            "moq": pa.get("moq"),
            "b2b_price_tiers": pa.get("b2b_price_tiers") or [],
            "fulfillment": fulfill,
            "price_details": {
                "currency": currency,
                "mrp_price": list_price,
                "current_price": current_price,
                "discount_percentage": discount_pct,
                "is_in_stock": in_stock,
                "stock_status_text": stock_text,
            },
            "short_description": specs.get("short_description"),
            "bullet_features": specs.get("bullet_features") or [],
            "specifications": specs.get("specifications") or {},
            "variants": specs.get("variants") or [],
            "images": media.get("images") or [],
            "videos": media.get("videos") or [],
            "images_count": len(media.get("images") or []),
            "videos_count": len(media.get("videos") or []),
            "seller_id": seller.get("seller_id") or r_meta.get("seller_id"),
            "seller_name": seller.get("seller_name"),
            "store_url": seller.get("store_url"),
            "is_official_brand_store": seller.get("is_official_brand_store"),
            "average_rating": avg_rating,
            "total_ratings": total_ratings,
            "total_written_reviews": total_written,
            "total_reviews": total_reviews,
            "recommended_percentage": pr_sum.get("recommended_percentage"),
            "star_distribution": star_dist,
            "five_star_count": star_dist["5"],
            "four_star_count": star_dist["4"],
            "three_star_count": star_dist["3"],
            "two_star_count": star_dist["2"],
            "one_star_count": star_dist["1"],
            "aspect_ratings": pr_sum.get("aspect_ratings") or {},
            "reviews_list": pr_items,
            "ratings_and_reviews": {
                "average_rating": avg_rating,
                "total_reviews": total_reviews,
                "star_distribution": star_dist,
                "five_star_count": star_dist["5"],
                "four_star_count": star_dist["4"],
            },
        }

    # Legacy v1 fallback
    pd = doc.get("price_details") or {}
    rar = doc.get("ratings_and_reviews") or {}
    dist = _normalize_star_distribution(rar.get("rating_counts") or rar.get("star_distribution"))
    cat_val = doc.get("category") or ""
    cat_path = [cat_val] if cat_val else []
    media_list = doc.get("mime_media") or []
    media_obj = doc.get("media") if isinstance(doc.get("media"), dict) else {}
    imgs = [m for m in media_list if isinstance(m, dict) and m.get("type") == "image"]
    vids = [m for m in media_list if isinstance(m, dict) and m.get("type") == "video"]
    if not imgs and not vids and media_obj:
        for u in media_obj.get("items") or []:
            u_str = u.get("url") if isinstance(u, dict) else str(u)
            if any(ext in u_str.lower() for ext in (".mp4", ".webm", ".mov")):
                vids.append({"url": u_str})
            elif u_str:
                imgs.append({"url": u_str})

    v1_reviews = list(doc.get("reviews") or [])
    if not v1_reviews and rar:
        for star_k in ("5", "4", "3", "2", "1"):
            for r in rar.get(star_k) or []:
                if isinstance(r, dict):
                    rc = dict(r)
                    rc.setdefault("rating", int(star_k))
                    v1_reviews.append(rc)

    return {
        "platform": doc.get("platform") or "samsung",
        "marketplace_type": get_platform_metadata(doc.get("platform") or "samsung")["marketplace_type"],
        "region": get_platform_metadata(doc.get("platform") or "samsung")["region"],
        "scraped_at": doc.get("scraped_at"),
        "schema_version": doc.get("schema_version") or "1.0.0",
        "product_id": doc.get("product_id") or "",
        "platform_product_id": doc.get("product_id") or "",
        "parent_product_id": None,
        "sku": doc.get("product_id"),
        "upc_ean": None,
        "model_number": None,
        "product_url": doc.get("product_url") or "",
        "product_name": doc.get("product_name") or doc.get("laptop_name") or "",
        "title": doc.get("product_name") or doc.get("laptop_name") or "",
        "brand": doc.get("brand") or "",
        "manufacturer": None,
        "category": cat_val,
        "category_path": cat_path,
        "condition": "new",
        "introduced_date": doc.get("introduced_date"),
        "currency": (pd.get("currency") or "INR").upper(),
        "list_price": pd.get("mrp_price") if pd.get("mrp_price") is not None else pd.get("original_price"),
        "current_price": pd.get("current_price"),
        "discount_percentage": pd.get("discount_percentage"),
        "is_on_sale": bool(pd.get("discount_percentage")),
        "in_stock": bool(pd.get("is_in_stock", False)),
        "stock_status_text": pd.get("stock_status_text"),
        "moq": 1,
        "b2b_price_tiers": [],
        "fulfillment": {},
        "price_details": pd,
        "short_description": None,
        "bullet_features": [],
        "specifications": {},
        "variants": [],
        "images": imgs,
        "videos": vids,
        "images_count": len(imgs),
        "videos_count": len(vids),
        "seller_id": None,
        "seller_name": None,
        "store_url": None,
        "is_official_brand_store": None,
        "average_rating": rar.get("average_rating"),
        "total_ratings": rar.get("total_reviews", 0) or 0,
        "total_written_reviews": len(v1_reviews),
        "total_reviews": rar.get("total_reviews", 0) or 0,
        "recommended_percentage": None,
        "star_distribution": dist,
        "five_star_count": dist["5"],
        "four_star_count": dist["4"],
        "three_star_count": dist["3"],
        "two_star_count": dist["2"],
        "one_star_count": dist["1"],
        "aspect_ratings": {},
        "reviews_list": v1_reviews,
        "ratings_and_reviews": rar,
    }


def _parse_v1_review_date(dt_raw: Optional[str]) -> Optional[str]:
    """Converts v1 human-readable date ('August 15, 2024') or ISO string to ISO UTC Z."""
    if not dt_raw or not isinstance(dt_raw, str):
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(dt_raw.strip(), fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return normalize_iso_utc(dt_raw)


def convert_v1_to_v2(v1_doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Migrates a legacy v1 product document into Schema v2.0.0 (product_doc, review_doc)."""
    if is_v2_product_doc(v1_doc):
        normalize_metadata_scraped_at(v1_doc)
        return v1_doc, build_review_v2(
            platform=(v1_doc.get("metadata") or {}).get("platform") or "generic",
            platform_product_id=((v1_doc.get("product") or {}).get("identifiers") or {}).get("platform_product_id"),
            scraped_at=(v1_doc.get("metadata") or {}).get("scraped_at"),
        )

    platform = (v1_doc.get("platform") or "samsung").lower()
    pid = v1_doc.get("product_id")
    pd = v1_doc.get("price_details") or {}
    media_list = v1_doc.get("mime_media") or []
    media_obj = v1_doc.get("media") if isinstance(v1_doc.get("media"), dict) else {}

    images: List[Dict[str, Any]] = []
    videos: List[Dict[str, Any]] = []
    for m in media_list:
        if not isinstance(m, dict):
            continue
        url = m.get("url")
        if not url:
            continue
        m_type = (m.get("type") or "").lower()
        if m_type == "video" or str(m.get("mime_type", "")).startswith("video/"):
            videos.append({"video_id": f"vid_{len(videos)+1:03d}", "url": url, "thumbnail_url": None, "duration_seconds": None})
        else:
            images.append({
                "url": url,
                "type": "main" if len(images) == 0 else "gallery",
                "position": len(images) + 1,
            })

    if not images and not videos and media_obj:
        for u in media_obj.get("items") or []:
            u_str = u.get("url") if isinstance(u, dict) else str(u)
            if not u_str:
                continue
            if any(ext in u_str.lower() for ext in (".mp4", ".webm", ".mov")):
                videos.append({"video_id": f"vid_{len(videos)+1:03d}", "url": u_str, "thumbnail_url": None, "duration_seconds": None})
            else:
                images.append({
                    "url": u_str,
                    "type": "main" if len(images) == 0 else "gallery",
                    "position": len(images) + 1,
                })

    cat = v1_doc.get("category")
    cat_path = [cat] if cat else []
    scraped_at = format_scraped_at_human_readable(v1_doc.get("scraped_at")) or now_human_readable()
    p_meta = get_platform_metadata(platform)
    is_official = platform == "samsung"
    title = v1_doc.get("product_name") or v1_doc.get("laptop_name") or ""
    list_price = pd.get("mrp_price") if pd.get("mrp_price") is not None else pd.get("original_price")

    product_v2 = build_product_v2(
        platform=platform,
        platform_product_id=pid,
        sku=pid,
        model_number=pid if (platform == "samsung" and pid and "-" in str(pid)) else None,
        product_url=v1_doc.get("product_url") or "",
        title=title,
        brand=v1_doc.get("brand"),
        manufacturer="Samsung Electronics" if (v1_doc.get("brand") or "").lower() == "samsung" else None,
        category_path=cat_path,
        condition="new",
        introduced_date=v1_doc.get("introduced_date"),
        currency=pd.get("currency") or p_meta["currency"],
        list_price=list_price,
        current_price=pd.get("current_price"),
        discount_percentage=pd.get("discount_percentage"),
        in_stock=bool(pd.get("is_in_stock", False)),
        stock_status_text=pd.get("stock_status_text"),
        images=images,
        videos=videos,
        seller_name="Samsung Official Store" if is_official else None,
        store_url=PLATFORM_CONFIGS.get(platform, {}).get("home_url") if is_official else None,
        is_official_brand_store=True if is_official else None,
        is_verified_seller=True if is_official else None,
        scraped_at=scraped_at,
    )

    rar = v1_doc.get("ratings_and_reviews") or {}
    dist = _normalize_star_distribution(rar.get("rating_counts") or rar.get("star_distribution"))
    raw_reviews = list(v1_doc.get("reviews") or [])
    if not raw_reviews and rar:
        for star_k in ("5", "4", "3", "2", "1"):
            for r in rar.get(star_k) or []:
                if isinstance(r, dict):
                    rc = dict(r)
                    rc.setdefault("rating", int(star_k))
                    raw_reviews.append(rc)

    review_items: List[Dict[str, Any]] = []
    for idx, r in enumerate(raw_reviews, start=1):
        if not isinstance(r, dict):
            continue
        ud = r.get("user_details") or {}
        pub_raw = r.get("published_at") or r.get("review_date") or r.get("submission_date")
        review_items.append({
            "review_id": r.get("review_id") or f"MIG_{pid or 'P'}_{idx}",
            "platform_product_id": pid,
            "rating": r.get("rating"),
            "title": r.get("title"),
            "body": r.get("body") or r.get("review_text") or r.get("comment"),
            "published_at": _parse_v1_review_date(pub_raw),
            "language": r.get("language"),
            "is_verified_purchase": r.get("is_verified_purchase"),
            "is_incentivized_or_default": r.get("is_incentivized_or_default", False),
            "is_recommended": r.get("is_recommended"),
            "helpful_votes": r.get("helpful_votes"),
            "reviewer": {
                "user_id": ud.get("user_id"),
                "name": ud.get("name") or ud.get("user_name"),
                "country_code": None,
                "badges": ud.get("badges") or [],
            },
            "purchased_variant": {"variant_id": None, "attributes_text": None},
            "tags": {"pros": [], "cons": []},
            "media": {"images": [], "videos": []},
            "comments": [],
        })

    review_v2 = build_review_v2(
        platform=platform,
        platform_product_id=pid,
        average_rating=rar.get("average_rating"),
        total_ratings=int(rar.get("total_reviews") or 0),
        total_written_reviews=len(review_items),
        star_distribution=dist,
        product_review_items=review_items,
        scraped_at=scraped_at,
    )
    return product_v2, review_v2


# Legacy dataclasses preserved for compatibility with existing callers/imports
@dataclass
class UserDetails:
    name: Optional[str] = None
    user_name: Optional[str] = None
    user_id: Optional[str] = None
    location: Optional[str] = None


@dataclass
class ReviewItem:
    review_id: Optional[str] = None
    rating: Optional[float] = None
    title: Optional[str] = None
    review_text: Optional[str] = None
    review_date: Optional[str] = None
    user_details: UserDetails = field(default_factory=UserDetails)


@dataclass
class PriceDetails:
    currency: str = "INR"
    current_price: Optional[float] = None
    original_price: Optional[float] = None
    mrp_price: Optional[float] = None
    discount_percentage: Optional[float] = None
    is_in_stock: bool = False
    stock_status_text: Optional[str] = None

    def __post_init__(self) -> None:
        if self.original_price is None and self.mrp_price is not None:
            self.original_price = self.mrp_price
        elif self.mrp_price is None and self.original_price is not None:
            self.mrp_price = self.original_price


@dataclass
class MediaObject:
    url: str
    mime_type: str
    type: str


@dataclass
class SamsungProduct:
    product_id: str
    product_name: str
    product_url: str
    platform: str = "samsung"
    brand: str = "Samsung"
    category: Optional[str] = None
    introduced_date: Optional[str] = None
    price_details: PriceDetails = field(default_factory=PriceDetails)
    mime_media: List[MediaObject] = field(default_factory=list)
    ratings_and_reviews: Dict[str, Any] = field(default_factory=dict)
    reviews: List[ReviewItem] = field(default_factory=list)
    scraped_at: str = field(default_factory=now_human_readable)
    v2_product: Optional[Dict[str, Any]] = None
    v2_review: Optional[Dict[str, Any]] = None

    def to_v2_dict(self) -> Dict[str, Any]:
        if self.v2_product:
            return self.v2_product
        prod_v2, _ = convert_v1_to_v2(asdict(self))
        return prod_v2

    def to_review_v2_dict(self) -> Dict[str, Any]:
        if self.v2_review:
            return self.v2_review
        _, rev_v2 = convert_v1_to_v2(asdict(self))
        return rev_v2

    def to_dict(self) -> Dict[str, Any]:
        return self.to_v2_dict()


Product = SamsungProduct
LaptopProduct = SamsungProduct
