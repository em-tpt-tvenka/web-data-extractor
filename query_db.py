"""
Unified Search & Filter Query Tool for Scraped Retail Products (MongoDB or JSON).
Supports filtering by platform, brand, category, search keywords, date range (default: past 1 year),
price range, stock status, minimum rating/review count, sorting, and exporting filtered results.
"""

import os
import sys
import json
import argparse
from typing import Any, Dict, List, Optional

from config import OUTPUT_JSON_PATH
from database import (
    MongoManager,
    query_json_with_reviews,
    save_laptops_to_json,
)
from models import extract_flat_summary, get_reviews_json_path, make_review_key
from scraper.filters import ProductFilter, add_filter_args, filter_from_args, format_price_str

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def get_collection():
    """Connect to MongoDB and return (MongoManager, collection)."""
    db = MongoManager()
    if not db.connect():
        print("❌ Could not connect to MongoDB at localhost:27017.")
        print("💡 Tip: Use `--source json --file output/samsung_products.json` to query local JSON directly.")
        sys.exit(1)
    return db, db._collection


def _describe_filter(flt: ProductFilter) -> str:
    """Build a concise human-readable summary of active filter parameters."""
    parts = []
    if flt.platform and flt.platform not in ("all", "auto"):
        parts.append(f"platform={flt.platform}")
    if flt.brand:
        parts.append(f"brand={flt.brand}")
    if flt.category and flt.category != "all":
        parts.append(f"category={flt.category}")
    if flt.search:
        parts.append(f"search='{flt.search}'")
    if flt.all_time:
        parts.append("date=all-time")
    elif flt.from_date or flt.to_date:
        parts.append(f"date={flt.from_date or '*'}..{flt.to_date or '*'}")
    elif flt.days is not None:
        parts.append(f"date=last {flt.days}d" + (" (+undated)" if flt.include_undated else ""))
    if flt.min_price is not None or flt.max_price is not None:
        parts.append(f"price={flt.min_price or 0:g}..{flt.max_price or '∞'}")
    if flt.min_rating is not None:
        parts.append(f"rating>={flt.min_rating}")
    if flt.min_reviews is not None:
        parts.append(f"reviews>={flt.min_reviews}")
    if flt.in_stock:
        parts.append("in_stock=True")
    if flt.sort_by:
        parts.append(f"sort={flt.sort_by}")
    if flt.limit:
        parts.append(f"limit={flt.limit}")
    return " | ".join(parts) if parts else "none"


def print_products_table(
    results: List[Dict[str, Any]],
    header_title: str,
    filter_desc: str = "",
    reviews_by_key: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> None:
    """Render matched Schema v2.0.0 (or legacy v1) products cleanly in an ASCII summary table."""
    width = 128
    print("=" * width)
    print(f" {header_title} | MATCHED: {len(results)} product(s)")
    if filter_desc:
        print(f" Active Filters: {filter_desc}")
    print("=" * width)

    if not results:
        print("No matching products found.")
        print("=" * width)
        return

    print(
        f"{'#':<3} {'PLATFORM':<14} {'BRAND':<10} {'CATEGORY':<13} {'PRODUCT ID':<17} "
        f"{'INTRO DATE':<15} {'PRICE':<12} {'REVIEWS':<12} {'PRODUCT NAME'}"
    )
    print("-" * width)

    rev_map = reviews_by_key or {}
    for idx, item in enumerate(results, 1):
        key = make_review_key(item)
        rev_doc = rev_map.get(key) or rev_map.get(key[1])
        flat = extract_flat_summary(item, rev_doc)

        plat = (flat["platform"] or "samsung")[:13]
        brand = (flat["brand"] or "Samsung")[:9]
        cat = (flat["category"] or "N/A")[:12]
        pid = (flat["product_id"] or "N/A")[:16]
        intro = (flat["introduced_date"] or "N/A")[:14]

        price_str = format_price_str(flat["price_details"])
        rev_count = flat["total_reviews"]
        avg_rat = flat["average_rating"]
        rating_str = f"{rev_count} ({avg_rat}★)" if avg_rat is not None else f"{rev_count}"

        name = (flat["title"] or "N/A")[:34]
        print(
            f"{idx:<3} {plat:<14} {brand:<10} {cat:<13} {pid:<17} "
            f"{intro:<15} {price_str:<12} {rating_str:<12} {name}"
        )

    print("-" * width)
    print("💡 TIP: Run `python query_db.py --id <PRODUCT_ID>` for complete Product + Review JSON details.")


def show_product_detail(product_id: str, source: str, filepath: str) -> None:
    """Display full Schema v2.0.0 Product Details + Review Details JSON for a product ID."""
    doc: Optional[Dict[str, Any]] = None
    rev_doc: Optional[Dict[str, Any]] = None

    if source == "json":
        all_items, rev_map = query_json_with_reviews(filepath, ProductFilter(all_time=True, include_undated=True))
        for item in all_items:
            key = make_review_key(item)
            if key[1].lower() == product_id.strip().lower():
                doc = item
                rev_doc = rev_map.get(key) or rev_map.get(key[1])
                break
    else:
        db, coll = get_collection()
        try:
            doc = coll.find_one(
                {
                    "$or": [
                        {"product.identifiers.platform_product_id": {"$regex": f"^{product_id}$", "$options": "i"}},
                        {"product_id": {"$regex": f"^{product_id}$", "$options": "i"}},
                    ]
                },
                {"_id": 0},
            )
            if db._reviews_collection is not None:
                rev_doc = db._reviews_collection.find_one(
                    {"metadata.platform_product_id": {"$regex": f"^{product_id}$", "$options": "i"}},
                    {"_id": 0},
                )
        finally:
            db.close()

    if not doc:
        print(f"❌ Product ID '{product_id}' not found in {source.upper()}.")
        return

    flat = extract_flat_summary(doc, rev_doc)
    print("=" * 80)
    print(f" 🔍 PRODUCT DETAILS JSON [v2.0.0] ({source.upper()}): [{flat['product_id']}]")
    print("=" * 80)
    print(json.dumps(doc, indent=2, ensure_ascii=False))

    if rev_doc:
        print("\n" + "=" * 80)
        print(f" ⭐ REVIEW DETAILS JSON [v2.0.0] ({source.upper()}): [{flat['product_id']}]")
        print("=" * 80)
        print(json.dumps(rev_doc, indent=2, ensure_ascii=False))


def _has_explicit_filter_flags(args: argparse.Namespace) -> bool:
    """Detect whether the user supplied any explicit filter/search flags."""
    if any(
        getattr(args, attr, None) is not None
        for attr in (
            "platform",
            "search",
            "category",
            "brand",
            "from_date",
            "to_date",
            "min_price",
            "max_price",
            "min_rating",
            "min_reviews",
            "sort_by",
            "limit",
        )
    ):
        return True
    if getattr(args, "in_stock", False) or getattr(args, "all_time", False) or getattr(args, "include_undated", False):
        return True
    # Check if --days was explicitly passed on command line
    return "--days" in sys.argv


def main() -> None:
    parser = argparse.ArgumentParser(description="Search & Filter Scraped Retail Products (MongoDB or JSON)")
    parser.add_argument(
        "--source",
        choices=["mongo", "json"],
        default=None,
        help="Data source to query: 'mongo' (default) or 'json'"
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help=f"JSON file path when querying JSON (default: {OUTPUT_JSON_PATH})"
    )
    parser.add_argument("--id", type=str, default=None, help="Show full JSON document for a specific product_id")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of results to display")
    parser.add_argument(
        "--export",
        type=str,
        nargs="?",
        const="output/filtered_products.json",
        default=None,
        help="Export matched results to a JSON + CSV file"
    )
    add_filter_args(parser, include_search=True)
    args = parser.parse_args()

    filepath = args.file or OUTPUT_JSON_PATH
    source = args.source or ("json" if args.file else "mongo")

    if args.id:
        show_product_detail(args.id, source=source, filepath=filepath)
        return

    # Bare invocation (`python query_db.py`) or bare `--export` shows full summary unless filters are passed
    if not _has_explicit_filter_flags(args):
        flt = ProductFilter(all_time=True, include_undated=True, limit=args.limit)
    else:
        flt = filter_from_args(args, for_query=True)

    if source == "json":
        results, reviews_by_key = query_json_with_reviews(filepath, flt)
        title = f"📁 JSON QUERY: '{filepath}'"
    else:
        db, _ = get_collection()
        try:
            results, reviews_by_key = db.query_products_and_reviews(flt)
            title = f"🍃 MONGODB QUERY: '{db.db_name}.{db.collection_name}'"
        finally:
            db.close()

    print_products_table(results, header_title=title, filter_desc=_describe_filter(flt), reviews_by_key=reviews_by_key)

    if args.export:
        if results:
            matched_reviews = [
                reviews_by_key[make_review_key(r)]
                for r in results
                if make_review_key(r) in reviews_by_key
            ]
            exported_path = save_laptops_to_json(results, args.export, merge=False, review_list=matched_reviews)
            print(f"✓ Exported {len(results)} product(s) to: '{exported_path}'")
            print(f"✓ Exported {len(matched_reviews)} review bundle(s) to: '{get_reviews_json_path(exported_path)}'")
        else:
            print("⚠️ No matching products to export.")


if __name__ == "__main__":
    main()
