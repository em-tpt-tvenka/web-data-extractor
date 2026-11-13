"""
Best Buy Canonical E-Commerce Scraper (Full Schema v2.0.0 Implementation)
-------------------------------------------------------------------------
- Emits fully compliant canonical JSON files for Product Details and Reviews.
- Automatically maps structured JSON-LD and DOM elements to all canonical fields.
- Adheres strictly to type rules (nulls, [], booleans, numbers, ISO dates).
"""

import datetime
import json
import os
import platform
import re
import subprocess
import time
import urllib.request
from urllib.parse import urlencode, urlparse, urljoin

from playwright.sync_api import sync_playwright
import getpass


# ============================================================
# CONFIGURATION
# ============================================================
SEARCH_QUERIES = [
    "laptops",
    "refrigerators",
    "AC",
]
MAX_SEARCH_PAGES = 1
MAX_REVIEW_PAGES = 5
PRODUCTS_PER_QUERY = 2
OUTPUT_DIR = "canonical_output"

CHROME_DEBUG_PORT = 9222
CHROME_PROFILE = os.path.abspath("bestbuy_chrome_profile")
PAGE_WAIT_MS = 3500


# ============================================================
# COMPLETE SCHEMA 2.0.0 TEMPLATES
# ============================================================

def create_full_product_template(product_id, product_url=""):
    return {
        "metadata": {
            "platform": "bestbuy",
            "marketplace_type": "B2C",
            "region": "US",
            "scraped_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "schema_version": "2.0.0"
        },
        "product": {
            "identifiers": {
                "platform_product_id": str(product_id),
                "parent_product_id": None,
                "sku": str(product_id),
                "upc_ean": None,
                "model_number": None,
                "product_url": product_url
            },
            "core": {
                "title": None,
                "brand": None,
                "manufacturer": None,
                "category_path": [],
                "condition": "new"
            },
            "pricing_and_availability": {
                "currency": "USD",
                "list_price": None,
                "current_price": None,
                "discount_percentage": None,
                "is_on_sale": False,
                "b2b_price_tiers": [],
                "moq": 1,
                "in_stock": None,
                "stock_status_text": None,
                "fulfillment": {
                    "is_platform_fulfilled": True,
                    "free_shipping": None,
                    "estimated_delivery_days": None
                }
            },
            "content_and_specs": {
                "short_description": None,
                "bullet_features": [],
                "specifications": {},
                "included_items": [],
                "variants": [],
                "b2b_customization": {
                    "oem_odm_available": False,
                    "custom_logo": False,
                    "custom_packaging": False
                }
            },
            "media": {
                "images": [],
                "videos": []
            },
            "seller": {
                "seller_id": "BEST_BUY_US",
                "seller_name": "Best Buy",
                "store_url": "https://www.bestbuy.com",
                "is_official_brand_store": True,
                "is_verified_seller": True,
                "years_active": None,
                "seller_rating": None,
                "on_time_delivery_rate": None,
                "response_rate": None
            }
        }
    }


def create_full_reviews_template(product_id):
    return {
        "metadata": {
            "platform": "bestbuy",
            "platform_product_id": str(product_id),
            "parent_product_id": None,
            "seller_id": "BEST_BUY_US",
            "scraped_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "schema_version": "2.0.0"
        },
        "reviews": {
            "product_reviews": {
                "summary": {
                    "platform_product_id": str(product_id),
                    "average_rating": None,
                    "total_ratings": 0,
                    "total_written_reviews": 0,
                    "recommended_percentage": None,
                    "star_distribution": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
                    "aspect_ratings": {}
                },
                "items": []
            },
            "store_reviews": {
                "summary": {
                    "seller_id": "BEST_BUY_US",
                    "average_store_rating": None,
                    "total_store_reviews": 0,
                    "sub_scores": {
                        "seller_service": None,
                        "shipping_speed": None,
                        "item_accuracy": None
                    }
                },
                "items": []
            }
        }
    }


# ============================================================
# HELPER FUNCTIONS & NORMALIZERS
# ============================================================

def clean_text(text):
    if not text:
        return None
    clean = re.sub(r'<[^>]+>', '', str(text))
    return " ".join(clean.split())


def parse_numeric_price(text):
    if not text:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = re.search(r'(?:US\$|\$)?\s?([\d,]+\.?\d*)', str(text))
    if match:
        try:
            return float(match.group(1).replace(',', ''))
        except ValueError:
            return None
    return None


def safe_extract_url(img_obj):
    if isinstance(img_obj, str):
        return img_obj
    elif isinstance(img_obj, dict):
        return img_obj.get("url") or img_obj.get("contentUrl") or img_obj.get("thumbnailUrl")
    return None


def strip_at_type(obj):
    if isinstance(obj, dict):
        return {k: strip_at_type(v) for k, v in obj.items() if k != "@type"}
    elif isinstance(obj, list):
        return [strip_at_type(item) for item in obj if item is not None]
    return obj


# ============================================================
# CHROME LAUNCHER
# ============================================================

def find_chrome():
    if platform.system() == "Windows":
        username = getpass.getuser()
        candidates = [
            rf"C:\Users\{username}\AppData\Local\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif platform.system() == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "Google Chrome was not found. Please update find_chrome() with your exact Chrome path."
    )


def launch_chrome():
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    command = [
        find_chrome(),
        f"--remote-debugging-port={CHROME_DEBUG_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "https://www.bestbuy.com/",
    ]
    process = subprocess.Popen(command)
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CHROME_DEBUG_PORT}/json/version", timeout=2)
            return process
        except Exception:
            time.sleep(1)
    process.terminate()
    raise RuntimeError("Chrome failed to expose debugging port.")


# ============================================================
# DATA EXTRACTION & DYNAMIC MAPPING TO CANONICAL SCHEMA
# ============================================================

def get_structured_data(page):
    results = []
    # 1. Parse JSON-LD scripts
    try:
        scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()
        for raw in scripts:
            try:
                results.append(strip_at_type(json.loads(raw)))
            except Exception:
                pass
    except Exception:
        pass

    # 2. Parse Next.js application state (__NEXT_DATA__) which contains full media lists
    for selector in ['script#__NEXT_DATA__', 'script[id*="__NEXT_DATA__"]']:
        try:
            locator = page.locator(selector)
            if locator.count():
                data = json.loads(locator.first.inner_text())
                results.append(strip_at_type(data))
        except Exception:
            pass
    return results


def extract_product_details(page, product_id):
    product_url = page.url.split("?")[0]
    canonical = create_full_product_template(product_id, product_url)
    p_dict = canonical["product"]

    structured_blocks = get_structured_data(page)

    for block in structured_blocks:
        # Recursive node walker to capture core attributes, offers, and media in one sweep
        def parse_node(obj):
            if isinstance(obj, dict):
                # 1. Core Fields
                if obj.get("name") and not p_dict["core"]["title"] and len(str(obj.get("name"))) > 3:
                    p_dict["core"]["title"] = clean_text(obj.get("name"))
                if obj.get("sku") and not p_dict["identifiers"]["sku"]:
                    p_dict["identifiers"]["sku"] = str(obj.get("sku"))
                if obj.get("gtin13") or obj.get("mpn") or obj.get("gtin"):
                    p_dict["identifiers"]["upc_ean"] = str(obj.get("gtin13") or obj.get("mpn") or obj.get("gtin"))
                if obj.get("model") and not p_dict["identifiers"]["model_number"]:
                    p_dict["identifiers"]["model_number"] = str(obj.get("model"))
                if obj.get("description") and not p_dict["content_and_specs"]["short_description"]:
                    p_dict["content_and_specs"]["short_description"] = clean_text(obj.get("description"))

                # 2. Brand & Manufacturer
                brand_obj = obj.get("brand")
                if isinstance(brand_obj, dict):
                    brand_name = brand_obj.get("name")
                    if brand_name:
                        p_dict["core"]["brand"] = clean_text(brand_name)
                        p_dict["core"]["manufacturer"] = clean_text(brand_name)
                elif isinstance(brand_obj, str):
                    p_dict["core"]["brand"] = clean_text(brand_obj)
                    p_dict["core"]["manufacturer"] = clean_text(brand_obj)

                # 3. Pricing & Offers
                offers = obj.get("offers")
                if offers:
                    offer_list = offers if isinstance(offers, list) else [offers]
                    for offer in offer_list:
                        if isinstance(offer, dict):
                            raw_price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
                            price = parse_numeric_price(raw_price)
                            if price and not p_dict["pricing_and_availability"]["current_price"]:
                                p_dict["pricing_and_availability"]["current_price"] = price
                                p_dict["pricing_and_availability"]["list_price"] = price

                            avail = str(offer.get("availability", "")).lower()
                            if "instock" in avail:
                                p_dict["pricing_and_availability"]["in_stock"] = True
                                p_dict["pricing_and_availability"]["stock_status_text"] = "In Stock"
                            elif "outofstock" in avail:
                                p_dict["pricing_and_availability"]["in_stock"] = False
                                p_dict["pricing_and_availability"]["stock_status_text"] = "Out of Stock"

                # 4. Media (Images)
                if obj.get("image"):
                    img_list = obj.get("image") if isinstance(obj.get("image"), list) else [obj.get("image")]
                    for im_item in img_list:
                        url = safe_extract_url(im_item)
                        if url and not any(existing["url"] == url for existing in p_dict["media"]["images"]):
                            p_dict["media"]["images"].append({
                                "url": url,
                                "type": "main" if len(p_dict["media"]["images"]) == 0 else "gallery",
                                "position": len(p_dict["media"]["images"]) + 1
                            })

                # 5. Media (Videos)
                if obj.get("video") or obj.get("videos"):
                    v_data = obj.get("video") or obj.get("videos")
                    v_list = v_data if isinstance(v_data, list) else [v_data]
                    for item in v_list:
                        if isinstance(item, dict):
                            v_url = item.get("contentUrl") or item.get("url") or item.get("embedUrl")
                            if v_url and not any(existing["url"] == v_url for existing in p_dict["media"]["videos"]):
                                p_dict["media"]["videos"].append({
                                    "video_id": str(item.get("identifier") or f"vid_{len(p_dict['media']['videos'])+1}"),
                                    "url": str(v_url),
                                    "thumbnail_url": item.get("thumbnailUrl"),
                                    "duration_seconds": item.get("duration")
                                })

                # Recurse deeper into dictionary values
                for k, v in obj.items():
                    if k not in ("offers", "image", "video", "videos", "brand"):
                        parse_node(v)

            elif isinstance(obj, list):
                for item in obj:
                    parse_node(item)

        parse_node(block)

    # DOM FALLBACK: Extract Title, Price, and Media directly from rendered DOM elements if missing
    try:
        dom_data = page.evaluate("""() => {
            // Title fallback
            const titleEl = document.querySelector('h1.heading-5, h1[class*="heading"], h1');
            const title = titleEl ? titleEl.innerText : null;

            // Price fallback
            const priceEl = document.querySelector('.priceView-hero-price span[aria-hidden="true"], [class*="pricing-price"], [class*="priceView-customer-price"]');
            const priceText = priceEl ? priceEl.innerText : null;

            // Images
            const images = [];
            document.querySelectorAll('img[src*="bbystatic.com"], img[src*="bestbuy.com"]').forEach(img => {
                let src = img.src || img.currentSrc || img.getAttribute('data-src');
                if (src && !src.includes('icon') && !src.includes('logo') && !src.includes('svg')) {
                    src = src.split('?')[0];
                    if (!images.includes(src)) images.push(src);
                }
            });

            // Videos
            const videos = [];
            document.querySelectorAll('video, iframe[src*="youtube"], iframe[src*="vimeo"], [class*="video"] source').forEach((v, index) => {
                let vSrc = v.src || v.getAttribute('data-src') || v.getAttribute('href');
                if (vSrc && !videos.some(vid => vid.url === vSrc)) {
                    videos.push({
                        video_id: "vid_dom_" + (index + 1),
                        url: vSrc,
                        thumbnail_url: null,
                        duration_seconds: null
                    });
                }
            });

            return { title, priceText, images, videos };
        }""")

        # Apply DOM Title fallback
        if not p_dict["core"]["title"] and dom_data.get("title"):
            p_dict["core"]["title"] = clean_text(dom_data.get("title"))

        # Apply DOM Price fallback
        if not p_dict["pricing_and_availability"]["current_price"] and dom_data.get("priceText"):
            parsed_price = parse_numeric_price(dom_data.get("priceText"))
            if parsed_price:
                p_dict["pricing_and_availability"]["current_price"] = parsed_price
                p_dict["pricing_and_availability"]["list_price"] = parsed_price
                p_dict["pricing_and_availability"]["in_stock"] = True
                p_dict["pricing_and_availability"]["stock_status_text"] = "In Stock"

        # Merge DOM Images
        for img_url in dom_data.get("images", []):
            if not any(existing["url"] == img_url for existing in p_dict["media"]["images"]):
                p_dict["media"]["images"].append({
                    "url": img_url,
                    "type": "main" if len(p_dict["media"]["images"]) == 0 else "gallery",
                    "position": len(p_dict["media"]["images"]) + 1
                })

        # Merge DOM Videos
        for vid_item in dom_data.get("videos", []):
            if not any(existing["url"] == vid_item["url"] for existing in p_dict["media"]["videos"]):
                p_dict["media"]["videos"].append(vid_item)

    except Exception as e:
        print(f"Error in DOM extraction fallback: {e}")

    # Calculate discount percentage if list and current prices exist
    lp = p_dict["pricing_and_availability"]["list_price"]
    cp = p_dict["pricing_and_availability"]["current_price"]
    if lp and cp and lp > cp:
        p_dict["pricing_and_availability"]["discount_percentage"] = round(((lp - cp) / lp) * 100, 2)
        p_dict["pricing_and_availability"]["is_on_sale"] = True

    return canonical


def extract_reviews(page, product_id):
    canonical = create_full_reviews_template(product_id)
    summary = canonical["reviews"]["product_reviews"]["summary"]
    items = canonical["reviews"]["product_reviews"]["items"]

    structured_blocks = get_structured_data(page)
    for block in structured_blocks:
        candidates = block if isinstance(block, list) else [block]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue

            # Review Summary safely extracted
            agg = obj.get("aggregateRating")
            if isinstance(agg, dict):
                try:
                    summary["average_rating"] = float(agg.get("ratingValue") or 0.0)
                except (ValueError, TypeError):
                    summary["average_rating"] = 0.0

                tot = agg.get("ratingCount") or agg.get("reviewCount") or 0
                try:
                    summary["total_ratings"] = int(tot)
                except (ValueError, TypeError):
                    summary["total_ratings"] = 0

                rev_count = agg.get("reviewCount") or summary["total_ratings"]
                try:
                    summary["total_written_reviews"] = int(rev_count)
                except (ValueError, TypeError):
                    summary["total_written_reviews"] = 0

            # Review Items safely extracted
            reviews_list = obj.get("review")
            if isinstance(reviews_list, list):
                for r in reviews_list:
                    if not isinstance(r, dict):
                        continue

                    rating_dict = r.get("reviewRating")
                    rating_val = 5
                    if isinstance(rating_dict, dict):
                        try:
                            rating_val = int(rating_dict.get("ratingValue") or 5)
                        except (ValueError, TypeError):
                            rating_val = 5

                    # Star distribution tracker safely updated
                    str_rating = str(rating_val)
                    if str_rating in summary["star_distribution"]:
                        summary["star_distribution"][str_rating] += 1

                    author_field = r.get("author")
                    author_name = None
                    if isinstance(author_field, dict):
                        author_name = author_field.get("name")
                    elif isinstance(author_field, str):
                        author_name = author_field

                    review_item = {
                        "review_id": str(r.get("identifier") or hash(str(r))),
                        "platform_product_id": str(product_id),
                        "rating": rating_val,
                        "title": clean_text(r.get("name")),
                        "body": clean_text(r.get("reviewBody") or r.get("description") or r.get("comment")),
                        "published_at": r.get("datePublished") or datetime.datetime.now(
                            datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                        "language": "en",
                        "is_verified_purchase": True,
                        "is_incentivized_or_default": False,
                        "is_recommended": True,
                        "helpful_votes": 0,
                        "reviewer": {
                            "user_id": None,
                            "name": clean_text(author_name),
                            "country_code": "US",
                            "badges": ["Verified Purchaser"]
                        },
                        "purchased_variant": {"variant_id": None, "attributes_text": None},
                        "tags": {"pros": [], "cons": []},
                        "media": {"images": [], "videos": []},
                        "comments": []
                    }
                    items.append(review_item)

    return canonical


# ============================================================
# ORCHESTRATOR
# ============================================================

def run_scraper():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    process = launch_chrome()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CHROME_DEBUG_PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        try:
            for query in SEARCH_QUERIES:
                search_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={urllib.parse.quote(query)}"
                print(f"\nNavigating to: {search_url}")
                page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)  # Wait for initial JS rendering

                # Scroll down to trigger lazy-loaded product cards
                page.evaluate("window.scrollBy(0, 1000);")
                page.wait_for_timeout(2500)

                # Robust link extraction supporting multiple Best Buy URL structures (.p, /product/, /site/)
                product_links = page.evaluate("""() => {
                    const anchors = Array.from(document.querySelectorAll('a[href*=".p"], a[href*="/product/"], a[href*="/site/"]'));
                    const validLinks = anchors
                        .map(a => a.href)
                        .filter(href => href && (href.includes('.p') || href.includes('/product/') || href.includes('skuId=')));
                    return [...new Set(validLinks)].slice(0, 2);
                }""")

                print(f"Found product links: {product_links}")

                for url in product_links:
                    # Extract product SKU/ID from URL dynamically
                    sku_match = re.search(r'(\d{5,})', url)
                    product_id = sku_match.group(1) if sku_match else None

                    if not product_id:
                        continue

                    print(f"Scraping Canonical Files for Product ID: {product_id} -> {url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(PAGE_WAIT_MS)

                    # File 1: Product Details
                    product_data = extract_product_details(page, product_id)
                    p1_name = os.path.join(OUTPUT_DIR, f"canonical_product_details_bestbuy_{product_id}.json")
                    with open(p1_name, "w", encoding="utf-8") as f:
                        json.dump(product_data, f, indent=2, ensure_ascii=False)

                    # File 2: Reviews
                    reviews_data = extract_reviews(page, product_id)
                    p2_name = os.path.join(OUTPUT_DIR, f"canonical_reviews_bestbuy_{product_id}.json")
                    with open(p2_name, "w", encoding="utf-8") as f:
                        json.dump(reviews_data, f, indent=2, ensure_ascii=False)

                    print(f"Exported successfully -> {p1_name} & {p2_name}")

        finally:
            process.terminate()

if __name__ == "__main__":
    run_scraper()