"""
Output Verification Script.
Queries MongoDB and inspects output/samsung_laptops.json,
displaying laptop details, pricing, introduction dates, MIME media, and user reviews.
"""

import os
import sys
import json
import argparse
from database import MongoManager

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def check_mongo(target_id: str = None):
    print("=" * 80)
    print(" 🍃 MONGODB DATABASE VERIFICATION")
    print("=" * 80)

    db = MongoManager()
    if not db.connect():
        print("❌ Could not connect to MongoDB. Make sure MongoDB service is running.")
        return

    laptops = db.get_all_laptops(limit=100)
    print(f"✓ Total laptops currently stored in MongoDB: {len(laptops)}\n")

    if not laptops:
        print("No documents found in collection 'laptops'. Run `python main.py` first.")
        db.close()
        return

    # Print summary table
    print(f"{'#':<3} {'PRODUCT ID':<18} {'INTRO DATE':<12} {'PRICE (INR)':<13} {'MEDIA':<18} {'REVIEWS':<10} {'NAME'}")
    print("-" * 105)

    for idx, lap in enumerate(laptops, 1):
        pid = lap.get("product_id", "N/A")
        name = lap.get("laptop_name", "N/A")[:30] + "..."
        intro = lap.get("introduced_date") or "N/A"
        price = lap.get("price_details", {}).get("current_price")
        price_str = f"₹{price:,.0f}" if price else "N/A"
        
        media = lap.get("media")
        if media:
            media_str = f"{media.get('images_count', 0)} img, {media.get('videos_count', 0)} vid"
        else:
            media_str = "null"

        revs = lap.get("reviews")
        rev_count = len(revs) if revs else 0
        rating = lap.get("rating", {})
        avg_rating = rating.get("average_rating") if rating else None
        rating_str = f"{rev_count} ({avg_rating}★)" if avg_rating else f"{rev_count}"

        print(f"{idx:<3} {pid:<18} {intro:<12} {price_str:<13} {media_str:<18} {rating_str:<10} {name}")

    print("-" * 105)

    # Detailed inspection of one product
    selected = None
    if target_id:
        selected = db.get_laptop_by_id(target_id.upper())
        if not selected:
            print(f"\n⚠️  Laptop with ID '{target_id}' not found. Showing first laptop instead.")
            selected = laptops[0]
    else:
        # Prefer showing one with reviews and videos if available
        for lap in laptops:
            if lap.get("reviews") and lap.get("media", {}).get("videos_count", 0) > 0:
                selected = lap
                break
        if not selected:
            selected = laptops[0]

    print("\n" + "=" * 80)
    print(f" 🔍 DETAILED DOCUMENT INSPECTION: [{selected.get('product_id')}]")
    print("=" * 80)
    print(f"Laptop Name      : {selected.get('laptop_name')}")
    print(f"Product URL      : {selected.get('product_url')}")
    print(f"Introduced Date  : {selected.get('introduced_date')}")
    print(f"Intro Metadata   : {json.dumps(selected.get('introduction_details'), indent=2)}")

    print(f"\n--- Price Details ---")
    p = selected.get("price_details", {})
    print(f"Current Price    : ₹{p.get('current_price', 0):,.2f}")
    print(f"Original / MRP   : ₹{p.get('original_price', 0):,.2f}")
    print(f"Discount Amount  : ₹{p.get('discount_amount', 0):,.2f} ({p.get('discount_percentage')}%)")
    print(f"In Stock Status  : {p.get('is_in_stock')}")

    print(f"\n--- Unified MIME Media Object ---")
    media = selected.get("media")
    if media:
        print(f"Total Items      : {media.get('total_items')}")
        print(f"Images Count     : {media.get('images_count')}")
        print(f"Videos Count     : {media.get('videos_count')}")
        
        raw_items = media.get("items", [])
        if raw_items:
            first_item = raw_items[0]
            first_url = first_item if isinstance(first_item, str) else first_item.get("url")
            print(f"Sample Media URL : {first_url}")
    else:
        print("Media            : null (no images or videos)")

    print(f"\n--- Rating (Past 1 Year) ---")
    r = selected.get("rating")
    if r:
        print(f"Average Rating   : {r.get('average_rating')} / 5")
        print(f"Past Year Reviews: {r.get('past_year_reviews_count')}")
        print(f"All Time Reviews : {r.get('total_all_time_reviews')}")
        print(f"Rating Breakdown : {r.get('rating_distribution')}")
    else:
        print("Rating           : null")

    print(f"\n--- User Reviews Sample (One Object Structure) ---")
    reviews = selected.get("reviews")
    if reviews:
        print(f"Total Past-Year Reviews: {len(reviews)}")
        print("\nFirst Review Object with Nested User Details:")
        print(json.dumps(reviews[0], indent=2, ensure_ascii=False))
    else:
        print("Reviews          : null")

    db.close()


def check_json(file_path: str = "output/samsung_laptops.json"):
    print("\n" + "=" * 80)
    print(" 📄 LOCAL JSON BACKUP VERIFICATION")
    print("=" * 80)
    if not os.path.exists(file_path):
        print(f"⚠️  JSON file not found at '{file_path}'. Run `python main.py` first.")
        return

    size_kb = os.path.getsize(file_path) / 1024
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"✓ JSON File Path : {os.path.abspath(file_path)}")
    print(f"✓ File Size      : {size_kb:.2f} KB")
    print(f"✓ Total Records  : {len(data)} laptops")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check MongoDB and JSON scraped output")
    parser.add_argument("--id", type=str, default=None, help="Inspect a specific product ID (e.g. --id NP960XJG-KG4IN)")
    args = parser.parse_args()

    check_mongo(target_id=args.id)
    check_json()

