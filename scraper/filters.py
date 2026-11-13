"""
Unified Search & Filtering Engine for Multi-Platform E-Commerce Products.
Shared across main.py (live scraping), browser_navigator.py, database.py, and query_db.py
so CLI flags, date cutoffs (e.g. last 1 year), price ranges, ratings, and search work uniformly.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from config import resolve_platform_name
from models import extract_flat_summary, make_review_key, is_v2_product_doc


DATE_FORMATS = (
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y/%m/%d",
)


def parse_date_string(date_str: Optional[str]) -> Optional[datetime]:
    """
    Parses human-readable or ISO date strings into a UTC-aware datetime.
    Supports formats like 'March 4, 2026', '2026-03-04', '2026-03-04T02:17:04Z'.
    """
    if not date_str or not isinstance(date_str, str):
        return None
    clean = date_str.split("T")[0].strip()
    # Handle ISO timestamp or date with time separated by space only if YYYY-MM-DD
    if len(clean) >= 10 and clean[4:5] == "-" and clean[7:8] == "-":
        clean = clean[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class ProductFilter:
    """Represents unified search & filter parameters across platforms."""
    platform: Optional[str] = None
    search: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    days: Optional[int] = 365
    all_time: bool = False
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    include_undated: bool = False
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_rating: Optional[float] = None
    min_reviews: Optional[int] = None
    in_stock: bool = False
    sort_by: Optional[str] = None
    limit: Optional[int] = None


def format_price_str(price_or_doc: Optional[Dict[str, Any]]) -> str:
    """Formats price_details OR a full Schema v2.0.0 product dict cleanly with its authentic currency symbol."""
    if is_v2_product_doc(price_or_doc):
        det = ((price_or_doc or {}).get("product") or {}).get("pricing_and_availability") or {}
    else:
        det = price_or_doc or {}
    val = det.get("current_price")
    if val is None:
        return "null"
    curr = (det.get("currency") or "USD").upper()
    sym = "$" if curr == "USD" else ("₹" if curr == "INR" else f"{curr} ")
    return f"{sym}{val:,.2f}" if (curr == "USD" and float(val) % 1 != 0) else f"{sym}{val:,.0f}"


def _matches_text(value: Optional[str], needle: str) -> bool:
    """Case-insensitive substring check."""
    return bool(value) and needle in value.lower()


def _find_review_doc(
    item: Dict[str, Any],
    reviews_by_key: Optional[Dict[Any, Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Finds paired Schema v2.0.0 Review Details doc by (platform, id) or id."""
    if not reviews_by_key:
        return None
    key = make_review_key(item)
    if key in reviews_by_key:
        return reviews_by_key[key]
    _, pid = key
    if pid and pid in reviews_by_key:
        return reviews_by_key[pid]
    return None


def _check_date_bounds(intro_date: Optional[str], flt: ProductFilter) -> Tuple[bool, str]:
    """Validates product introduced_date against --days, --all-time, and custom date bounds."""
    has_custom_range = bool(flt.from_date or flt.to_date)
    if flt.all_time and not has_custom_range:
        return True, ""

    if not intro_date:
        if flt.include_undated:
            return True, ""
        return False, "no introduced_date (use --all-time or --include-undated to keep)"

    dt = parse_date_string(intro_date)
    if dt is None:
        return (True, "") if flt.include_undated else (False, f"unparseable date '{intro_date}'")

    if flt.from_date:
        from_dt = parse_date_string(flt.from_date)
        if from_dt and dt < from_dt:
            return False, f"introduced '{intro_date}' is before {flt.from_date}"

    if flt.to_date:
        to_dt = parse_date_string(flt.to_date)
        if to_dt and dt > to_dt.replace(hour=23, minute=59, second=59):
            return False, f"introduced '{intro_date}' is after {flt.to_date}"

    if not flt.all_time and flt.days is not None and flt.days > 0 and not flt.from_date:
        cutoff = datetime.now(timezone.utc) - timedelta(days=flt.days)
        if dt < cutoff:
            return False, f"introduced '{intro_date}' is older than {flt.days} days"

    return True, ""


def _check_price_and_stock(flat: Dict[str, Any], flt: ProductFilter) -> Tuple[bool, str]:
    """Validates min_price, max_price, and in_stock filters on a normalized flat summary."""
    cur_price = flat.get("current_price")

    if flt.in_stock and not flat.get("in_stock", False):
        return False, "out of stock or unavailable online"

    if flt.min_price is not None:
        if cur_price is None or cur_price < flt.min_price:
            return False, f"price {cur_price} < min_price {flt.min_price}"

    if flt.max_price is not None:
        if cur_price is None or cur_price > flt.max_price:
            return False, f"price {cur_price} > max_price {flt.max_price}"

    return True, ""


def _check_rating_and_reviews(flat: Dict[str, Any], flt: ProductFilter) -> Tuple[bool, str]:
    """Validates min_rating and min_reviews filters on a normalized flat summary."""
    avg_rating = flat.get("average_rating")
    total_reviews = flat.get("total_reviews", 0) or 0

    if flt.min_rating is not None:
        if avg_rating is None or float(avg_rating) < flt.min_rating:
            return False, f"rating {avg_rating} < min_rating {flt.min_rating}"

    if flt.min_reviews is not None and int(total_reviews) < flt.min_reviews:
        return False, f"reviews {total_reviews} < min_reviews {flt.min_reviews}"

    return True, ""


_SEARCH_CATEGORY_SYNONYMS = {
    "laptop": ("laptop", "laptops", "galaxy book", "galaxy-book", "notebook", "computers", "macbook", "chromebook", "thinkpad", "ultrabook"),
    "laptops": ("laptop", "laptops", "galaxy book", "galaxy-book", "notebook", "computers", "macbook", "chromebook", "thinkpad", "ultrabook"),
    "notebook": ("notebook", "laptop", "galaxy book", "computers"),
    "phone": ("phone", "phones", "smartphone", "smartphones", "mobile", "galaxy s", "galaxy z", "galaxy a", "galaxy m", "galaxy f", "iphone"),
    "phones": ("phone", "phones", "smartphone", "smartphones", "mobile", "galaxy s", "galaxy z", "galaxy a", "galaxy m", "galaxy f", "iphone"),
    "smartphone": ("smartphone", "smartphones", "phone", "mobile", "galaxy s", "galaxy z", "galaxy a", "galaxy m", "galaxy f", "iphone"),
    "smartphones": ("smartphone", "smartphones", "phone", "mobile", "galaxy s", "galaxy z", "galaxy a", "galaxy m", "galaxy f", "iphone"),
    "mobile": ("mobile", "smartphone", "smartphones", "phone", "galaxy s", "galaxy z", "galaxy a", "galaxy m", "galaxy f"),
    "mobiles": ("mobile", "smartphone", "smartphones", "phone", "galaxy s", "galaxy z", "galaxy a", "galaxy m", "galaxy f"),
    "tv": ("tv", "tvs", "television", "qled", "oled", "neo qled", "crystal uhd", "bravia"),
    "tvs": ("tv", "tvs", "television", "qled", "oled", "neo qled", "crystal uhd", "bravia"),
    "television": ("television", "tv", "tvs", "qled", "oled"),
    "televisions": ("television", "tv", "tvs", "qled", "oled"),
    "tablet": ("tablet", "tablets", "galaxy tab", "tab", "ipad"),
    "tablets": ("tablet", "tablets", "galaxy tab", "tab", "ipad"),
    "watch": ("watch", "watches", "galaxy watch", "smartwatch"),
    "watches": ("watch", "watches", "galaxy watch", "smartwatch"),
    "fridge": ("fridge", "refrigerator", "refrigerators"),
    "fridges": ("fridge", "refrigerator", "refrigerators"),
    "refrigerator": ("refrigerator", "refrigerators", "fridge"),
    "refrigerators": ("refrigerator", "refrigerators", "fridge"),
    "earbuds": ("earbuds", "buds", "galaxy buds", "audio"),
    "buds": ("buds", "earbuds", "galaxy buds", "audio"),
}


def _token_matches_searchable(tok: str, searchable: str) -> bool:
    """Checks exact substring, plural stem, or known retail category synonyms."""
    if tok in searchable:
        return True
    if tok in _SEARCH_CATEGORY_SYNONYMS:
        if any(syn in searchable for syn in _SEARCH_CATEGORY_SYNONYMS[tok]):
            return True
    if len(tok) > 3 and tok.endswith("s") and tok[:-1] in searchable:
        return True
    return False


def matches_filter(
    item: Dict[str, Any],
    flt: Optional[ProductFilter],
    review_doc: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Evaluates whether a Schema v2.0.0 (or legacy v1) product dict and optional paired review doc
    satisfy all active search & filter criteria. Returns (True, "") if matched, or (False, reason).
    """
    if flt is None:
        return True, ""

    flat = extract_flat_summary(item, review_doc)

    # 1. Platform filter
    if flt.platform and flt.platform.lower() not in ("all", "auto", "generic"):
        item_platform = (flat.get("platform") or "samsung").lower()
        if item_platform != flt.platform.lower():
            return False, f"platform '{item_platform}' != '{flt.platform}'"

    # 2. Brand filter
    if flt.brand and flt.brand.lower() != "all":
        item_brand = (flat.get("brand") or "").lower()
        if flt.brand.lower() not in item_brand:
            return False, f"brand '{flat.get('brand')}' does not match '{flt.brand}'"

    # 3. Category filter (checks full category_path breadcrumbs)
    if flt.category and flt.category.lower() != "all":
        cat_joined = " ".join(str(c) for c in (flat.get("category_path") or [])).lower()
        item_cat = f"{flat.get('category') or ''} {cat_joined}".lower()
        cat_q = flt.category.lower().rstrip("s")
        if cat_q not in item_cat and flt.category.lower() not in item_cat:
            return False, f"category '{item_cat.strip()}' does not match '{flt.category}'"

    # 4. Keyword search across title, description, identifiers, brand, category, url, seller
    if flt.search:
        short_desc = ""
        if isinstance(item.get("product"), dict):
            short_desc = str((item["product"].get("content_and_specs") or {}).get("short_description") or "")
        searchable = " ".join([
            str(flat.get("title") or ""),
            short_desc,
            str(flat.get("product_id") or ""),
            str(flat.get("sku") or ""),
            str(flat.get("model_number") or ""),
            str(flat.get("brand") or ""),
            str(flat.get("manufacturer") or ""),
            " ".join(str(c) for c in (flat.get("category_path") or [])),
            str(flat.get("product_url") or ""),
            str(flat.get("seller_name") or ""),
        ]).lower()
        tokens = [t.strip().lower() for t in flt.search.split() if t.strip()]
        if tokens and not all(_token_matches_searchable(tok, searchable) for tok in tokens):
            return False, f"does not match search '{flt.search}'"

    # 5. Date, price, stock, and rating checks
    ok, reason = _check_date_bounds(flat.get("introduced_date"), flt)
    if not ok:
        return False, reason

    ok, reason = _check_price_and_stock(flat, flt)
    if not ok:
        return False, reason

    ok, reason = _check_rating_and_reviews(flat, flt)
    if not ok:
        return False, reason

    return True, ""


def _sort_products(
    items: List[Dict[str, Any]],
    sort_by: Optional[str],
    reviews_by_key: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Sorts filtered Schema v2.0.0 (or legacy v1) product dicts safely while placing missing values last."""
    if not sort_by:
        return items

    def _flat(x: Dict[str, Any]) -> Dict[str, Any]:
        return extract_flat_summary(x, _find_review_doc(x, reviews_by_key))

    key = sort_by.lower()
    if key == "price_asc":
        return sorted(items, key=lambda x: (
            _flat(x).get("current_price") is None,
            _flat(x).get("current_price") or 0.0,
        ))
    if key == "price_desc":
        return sorted(items, key=lambda x: (
            _flat(x).get("current_price") is not None,
            _flat(x).get("current_price") or 0.0,
        ), reverse=True)
    if key == "rating_desc":
        return sorted(items, key=lambda x: (
            _flat(x).get("average_rating") is not None,
            float(_flat(x).get("average_rating") or 0.0),
        ), reverse=True)
    if key == "reviews_desc":
        return sorted(items, key=lambda x: int(_flat(x).get("total_reviews") or 0), reverse=True)
    if key == "date_desc":
        return sorted(items, key=lambda x: (
            parse_date_string(_flat(x).get("introduced_date")) is not None,
            parse_date_string(_flat(x).get("introduced_date")) or datetime.min.replace(tzinfo=timezone.utc),
        ), reverse=True)
    if key == "name_asc":
        return sorted(items, key=lambda x: (_flat(x).get("title") or "").lower())
    return items


def filter_and_sort_products(
    items: List[Dict[str, Any]],
    flt: Optional[ProductFilter],
    reviews_by_key: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Filters, sorts, and limits a list of product dicts using ProductFilter + optional paired reviews."""
    if flt is None:
        return items

    matched = [
        item for item in items
        if matches_filter(item, flt, review_doc=_find_review_doc(item, reviews_by_key))[0]
    ]
    sorted_items = _sort_products(matched, flt.sort_by, reviews_by_key=reviews_by_key)

    if flt.limit and flt.limit > 0:
        return sorted_items[:flt.limit]
    return sorted_items


def to_mongo_query(flt: Optional[ProductFilter]) -> Dict[str, Any]:
    """Translates compatible scalar filters into a MongoDB find() query for Schema v2.0.0 products."""
    if flt is None:
        return {}

    query: Dict[str, Any] = {}
    if flt.platform and flt.platform.lower() not in ("all", "auto", "generic"):
        query["metadata.platform"] = {"$regex": f"^{flt.platform}$", "$options": "i"}
    if flt.brand and flt.brand.lower() != "all":
        query["product.core.brand"] = {"$regex": flt.brand, "$options": "i"}
    if flt.category and flt.category.lower() != "all":
        query["product.core.category_path"] = {"$regex": flt.category.rstrip("s"), "$options": "i"}
    if flt.in_stock:
        query["product.pricing_and_availability.in_stock"] = True

    price_cond: Dict[str, Any] = {}
    if flt.min_price is not None:
        price_cond["$gte"] = flt.min_price
    if flt.max_price is not None:
        price_cond["$lte"] = flt.max_price
    if price_cond:
        query["product.pricing_and_availability.current_price"] = price_cond

    # Note: min_rating and min_reviews are evaluated via paired Schema 2 `reviews` collection in query_products().
    return query


def add_filter_args(parser: argparse.ArgumentParser, include_search: bool = True) -> None:
    """Registers consistent search and filter CLI arguments on an ArgumentParser."""
    parser.add_argument(
        "--platform",
        type=str,
        default=None,
        help="Retail platform (name or alias): samsung, croma, reliancedigital, vijaysales, flipkart, amazon, alibaba, walmart, jiomart, bestbuy, ebay, target, indiamart, generic, auto, all"
    )
    if include_search:
        parser.add_argument(
            "--search",
            type=str,
            default=None,
            help="Search query / keyword across product name, model ID, category, or retail site"
        )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Category filter (smartphones, laptops, tvs, tablets, watches, refrigerators, audio, all)"
    )
    parser.add_argument(
        "--brand",
        type=str,
        default=None,
        help="Brand filter (e.g. Samsung, Apple, HP, Dell, Sony, LG)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Include products/reviews from last N days (default: 365 for last 1 year)"
    )
    parser.add_argument(
        "--all-time",
        action="store_true",
        help="Disable date cutoff filter (include products from all time)"
    )
    parser.add_argument(
        "--from-date",
        type=str,
        default=None,
        help="Filter products introduced on/after YYYY-MM-DD"
    )
    parser.add_argument(
        "--to-date",
        type=str,
        default=None,
        help="Filter products introduced on/before YYYY-MM-DD"
    )
    parser.add_argument(
        "--include-undated",
        action="store_true",
        help="Keep products whose webpage omits a launch/publish date even when --days is set"
    )
    parser.add_argument("--min-price", type=float, default=None, help="Minimum current price")
    parser.add_argument("--max-price", type=float, default=None, help="Maximum current price")
    parser.add_argument("--min-rating", type=float, default=None, help="Minimum average rating (0.0 to 5.0)")
    parser.add_argument("--min-reviews", type=int, default=None, help="Minimum review count")
    parser.add_argument("--in-stock", action="store_true", help="Only include products currently in stock")
    parser.add_argument(
        "--sort-by",
        type=str,
        choices=["price_asc", "price_desc", "rating_desc", "reviews_desc", "date_desc", "name_asc"],
        default=None,
        help="Sort results by price, rating, reviews, date, or name"
    )


def filter_from_args(args: argparse.Namespace, for_query: bool = False) -> ProductFilter:
    """Builds a ProductFilter from parsed CLI arguments."""
    platform_val = getattr(args, "platform", None)
    if platform_val:
        platform_val = resolve_platform_name(platform_val)
    # When scraping, default platform is 'samsung' if omitted; when querying DB/JSON, default None (all)
    if not for_query and not platform_val:
        platform_val = "auto" if (getattr(args, "url", None) or getattr(args, "urls", None)) else "samsung"

    return ProductFilter(
        platform=platform_val,
        search=getattr(args, "search", None),
        category=getattr(args, "category", None),
        brand=getattr(args, "brand", None),
        days=getattr(args, "days", 365),
        all_time=bool(getattr(args, "all_time", False)),
        from_date=getattr(args, "from_date", None),
        to_date=getattr(args, "to_date", None),
        include_undated=bool(getattr(args, "include_undated", False)),
        min_price=getattr(args, "min_price", None),
        max_price=getattr(args, "max_price", None),
        min_rating=getattr(args, "min_rating", None),
        min_reviews=getattr(args, "min_reviews", None),
        in_stock=bool(getattr(args, "in_stock", False)),
        sort_by=getattr(args, "sort_by", None),
        limit=getattr(args, "limit", None)
    )
