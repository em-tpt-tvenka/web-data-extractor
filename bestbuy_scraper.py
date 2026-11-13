"""
Best Buy Canonical E-Commerce Scraper (Standardized Schema Implementation with Strict Types)
-------------------------------------------------------------------------------------------
- Emits fully compliant standardized JSON records for Product Details and Reviews.
- Dynamically maps structured JSON-LD, __NEXT_DATA__, and DOM elements.
- Enforces strict native data types (float, int, bool, list, dict, null) without hardcoding.
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
    "Air Conditioner",
]
MAX_SEARCH_PAGES = 3
MAX_REVIEW_PAGES = 5
PRODUCTS_PER_QUERY = 10
OUTPUT_DIR = "canonical_output"

CHROME_DEBUG_PORT = 9222
CHROME_PROFILE = os.path.abspath("bestbuy_chrome_profile")
PAGE_WAIT_MS = 3500


# ============================================================
# STANDARDIZED TEMPLATES WITH STRICT DATA TYPES
# ============================================================

def create_standardized_product_record(product_id, product_url=""):
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "_id": str(f"tenant_001:bestbuy:product_{product_id}"),
        "schema_version": str("1.0"),
        "record_type": str("product_catalog"),
        "tenant": {
            "tenant_id": str("tenant_001"),
            "project_id": str("retail_listening_001"),
            "report_scope_ids": [str("TND_Q2_FY27")]
        },
        "source": {
            "platform": str("bestbuy"),
            "channel": str("ecommerce_catalog"),
            "source_type": str("product_page"),
            "source_item_id": str(product_id),
            "source_url": str(product_url),
            "parent_item_id": None,  # type: str | None
            "published_at": None,  # type: str | None
            "collected_at": str(now_iso),
            "updated_at": None,  # type: str | None
            "country_code": str("US"),
            "marketplace": str("bestbuy.com")
        },
        "content": {
            "title": None,  # type: str | None
            "text": None,  # type: str | None
            "language": str("en"),
            "original_language": str("en"),
            "content_type": str("product_description"),
            "media": {
                "images": [],  # type: list
                "videos": []  # type: list
            },
            "hashtags": [],  # type: list
            "mentions": [],  # type: list
            "keywords": []  # type: list
        },
        "entities": {
            "brands": [],  # type: list
            "products": [
                {
                    "product_id": str(product_id),
                    "product_name": None,  # type: str | None
                    "sku": str(product_id),
                    "model_number": None,  # type: str | None
                    "product_url": str(product_url)
                }
            ],
            "retailer": {
                "retailer_id": str("BEST_BUY_US"),
                "retailer_name": str("Best Buy")
            },
            "other_brands": []  # type: list
        },
        "taxonomy": {
            "industry": str("Tech & Durables"),
            "category": str("Large White Goods / Electronics"),
            "subcategory": str("Appliance / Computer"),
            "product_type": str("Standard"),
            "taxonomy_version": str("TND_2026_V1"),
            "mapping_method": str("catalog_mapping"),
            "mapping_status": str("mapped")
        },
        "product_attributes": {
            "model_number": None,  # type: str | None
            "capacity": None,  # type: dict | None
            "color": None,  # type: str | None
            "energy_rating": None,  # type: str | None
            "specifications": {}  # type: dict
        },
        "commercial": {
            "price": {
                "listed_price": {
                    "amount": None,  # type: float | None
                    "currency": str("USD")
                },
                "selling_price": {
                    "amount": None,  # type: float | None
                    "currency": str("USD")
                },
                "original_price": {
                    "amount": None,  # type: float | None
                    "currency": str("USD")
                },
                "discount_amount": None,  # type: float | None
                "discount_percentage": None,  # type: float | None
                "price_type": str("standard"),
                "price_observed_at": str(now_iso)
            },
            "price_range": {
                "minimum": None,  # type: float | None
                "maximum": None,  # type: float | None
                "currency": str("USD"),
                "period_start": None,  # type: str | None
                "period_end": None  # type: str | None
            },
            "promotion": {
                "is_promotional": False,  # type: bool
                "promotion_type": None,  # type: str | None
                "promotion_name": None,  # type: str | None
                "coupon_available": False,  # type: bool
                "coupon_code": None  # type: str | None
            },
            "payment_options": {
                "emi_available": True,  # type: bool
                "no_cost_emi": False,  # type: bool
                "bank_offer_available": False,  # type: bool
                "exchange_offer_available": True,  # type: bool
                "cash_on_delivery": False  # type: bool
            },
            "availability": {
                "status": None,  # type: str | None
                "stock_quantity": None  # type: int | None
            },
            "delivery": {
                "delivery_charge": {
                    "amount": float(0.0),  # type: float
                    "currency": str("USD")
                },
                "estimated_delivery_date": None,  # type: str | None
                "delivery_type": str("standard_delivery")
            },
            "warranty": {
                "duration_months": int(12),  # type: int
                "warranty_type": str("manufacturer")
            }
        },
        "metrics": {
            "rating": {
                "value": None,  # type: float | None
                "scale_min": int(1),  # type: int
                "scale_max": int(5)  # type: int
            },
            "engagement": {
                "likes": None,  # type: int | None
                "comments": None,  # type: int | None
                "shares": None,  # type: int | None
                "views": None,  # type: int | None
                "reactions": None  # type: int | None
            },
            "helpful_votes": None,  # type: int | None
            "review_count": int(0),  # type: int
            "author_followers": None,  # type: int | None
            "reach": None  # type: int | None
        },
        "author": {
            "source_author_id_hash": None,  # type: str | None
            "display_name": str("Best Buy"),
            "author_type": str("merchant"),
            "is_verified_purchase": False,  # type: bool
            "profile_url": str("https://www.bestbuy.com"),
            "follower_count": None  # type: int | None
        },
        "location": {
            "country_code": str("US"),
            "region": None,  # type: str | None
            "city": None,  # type: str | None
            "location_precision": str("country")
        },
        "campaign": {
            "campaign_name": None,  # type: str | None
            "campaign_type": None,  # type: str | None
            "event_name": None,  # type: str | None
            "campaign_id": None  # type: str | None
        },
        "deduplication": {
            "content_hash": None,  # type: str | None
            "duplicate_status": str("unique"),
            "canonical_record_id": str(f"tenant_001:bestbuy:product_{product_id}"),
            "duplicate_of": None  # type: str | None
        },
        "provenance": {
            "collection_run_id": str("RUN_20261113_001"),
            "connector_name": str("bestbuy_connector"),
            "connector_version": str("1.0.0"),
            "normalizer_version": str("1.0.0"),
            "source_payload_ref": None,  # type: str | None
            "ingestion_timestamp": str(now_iso)
        },
        "quality": {
            "status": str("accepted"),
            "is_valid": True,  # type: bool
            "is_relevant": True,  # type: bool
            "validation_errors": [],  # type: list
            "missing_optional_fields": []  # type: list
        },
        "lifecycle": {
            "first_seen_at": str(now_iso),
            "last_seen_at": str(now_iso),
            "record_status": str("active")
        }
    }


def create_standardized_reviews_container(product_id):
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "metadata": {
            "tenant_id": str("tenant_001"),
            "platform": str("bestbuy"),
            "platform_product_id": str(product_id),
            "scraped_at": str(now_iso),
            "schema_version": str("1.0")
        },
        "reviews_summary": {
            "average_rating": None,  # type: float | None
            "total_ratings": int(0),  # type: int
            "total_written_reviews": int(0),  # type: int
            "star_distribution": {"1": int(0), "2": int(0), "3": int(0), "4": int(0), "5": int(0)}
        },
        "items": []  # type: list
    }


def create_standardized_review_item(product_id, review_id, product_url=""):
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "_id": str(f"tenant_001:bestbuy:review_{product_id}_{review_id}"),
        "schema_version": str("1.0"),
        "record_type": str("social_mention"),
        "tenant": {
            "tenant_id": str("tenant_001"),
            "project_id": str("retail_listening_001"),
            "report_scope_ids": [str("TND_Q2_FY27")]
        },
        "source": {
            "platform": str("bestbuy"),
            "channel": str("ecommerce_review"),
            "source_type": str("review"),
            "source_item_id": str(review_id),
            "source_url": str(product_url),
            "parent_item_id": str(product_id),
            "published_at": str(now_iso),
            "collected_at": str(now_iso),
            "updated_at": None,
            "country_code": str("US"),
            "marketplace": str("bestbuy.com")
        },
        "content": {
            "title": None,
            "text": None,
            "language": str("en"),
            "original_language": str("en"),
            "content_type": str("review"),
            "media": {
                "images": [],
                "videos": []
            },
            "hashtags": [],
            "mentions": [],
            "keywords": []
        },
        "entities": {
            "brands": [],
            "products": [
                {
                    "product_id": str(product_id),
                    "product_name": None,
                    "sku": str(product_id),
                    "model_number": None,
                    "product_url": str(product_url)
                }
            ],
            "retailer": {
                "retailer_id": str("BEST_BUY_US"),
                "retailer_name": str("Best Buy")
            },
            "other_brands": []
        },
        "taxonomy": {
            "industry": str("Tech & Durables"),
            "category": str("Reviews"),
            "subcategory": str("Customer Review"),
            "product_type": str("Standard"),
            "taxonomy_version": str("TND_2026_V1"),
            "mapping_method": str("catalog_mapping"),
            "mapping_status": str("mapped")
        },
        "product_attributes": {
            "model_number": None,
            "capacity": None,
            "color": None,
            "energy_rating": None,
            "specifications": {}
        },
        "commercial": {
            "price": {
                "listed_price": {"amount": None, "currency": str("USD")},
                "selling_price": {"amount": None, "currency": str("USD")},
                "original_price": {"amount": None, "currency": str("USD")},
                "discount_amount": None,
                "discount_percentage": None,
                "price_type": str("standard"),
                "price_observed_at": str(now_iso)
            },
            "price_range": {"minimum": None, "maximum": None, "currency": str("USD"), "period_start": None,
                            "period_end": None},
            "promotion": {"is_promotional": False, "promotion_type": None, "promotion_name": None,
                          "coupon_available": False, "coupon_code": None},
            "payment_options": {"emi_available": False, "no_cost_emi": False, "bank_offer_available": False,
                                "exchange_offer_available": False, "cash_on_delivery": False},
            "availability": {"status": None, "stock_quantity": None},
            "delivery": {"delivery_charge": {"amount": float(0.0), "currency": str("USD")},
                         "estimated_delivery_date": None, "delivery_type": None},
            "warranty": {"duration_months": None, "warranty_type": None}
        },
        "metrics": {
            "rating": {
                "value": int(5),
                "scale_min": int(1),
                "scale_max": int(5)
            },
            "engagement": {"likes": None, "comments": None, "shares": None, "views": None, "reactions": None},
            "helpful_votes": int(0),
            "review_count": None,
            "author_followers": None,
            "reach": None
        },
        "author": {
            "source_author_id_hash": None,
            "display_name": None,
            "author_type": str("customer"),
            "is_verified_purchase": True,
            "profile_url": None,
            "follower_count": None
        },
        "location": {"country_code": str("US"), "region": None, "city": None, "location_precision": str("country")},
        "campaign": {"campaign_name": None, "campaign_type": None, "event_name": None, "campaign_id": None},
        "deduplication": {"content_hash": None, "duplicate_status": str("unique"),
                          "canonical_record_id": str(f"tenant_001:bestbuy:review_{product_id}_{review_id}"),
                          "duplicate_of": None},
        "provenance": {
            "collection_run_id": str("RUN_20261113_001"),
            "connector_name": str("bestbuy_connector"),
            "connector_version": str("1.0.0"),
            "normalizer_version": str("1.0.0"),
            "source_payload_ref": None,
            "ingestion_timestamp": str(now_iso)
        },
        "quality": {"status": str("accepted"), "is_valid": True, "is_relevant": True, "validation_errors": [],
                    "missing_optional_fields": []},
        "lifecycle": {
            "first_seen_at": str(now_iso),
            "last_seen_at": str(now_iso),
            "record_status": str("active")
        }
    }


# ============================================================
# HELPER FUNCTIONS & NORMALIZERS
# ============================================================

def clean_text(text):
    if not text:
        return None
    clean = re.sub(r'<[^>]+>', '', str(text))
    return str(" ".join(clean.split()))


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
        return str(img_obj)
    elif isinstance(img_obj, dict):
        url = img_obj.get("url") or img_obj.get("contentUrl") or img_obj.get("thumbnailUrl")
        return str(url) if url else None
    return None


def strip_at_type(obj):
    if isinstance(obj, dict):
        return {str(k): strip_at_type(v) for k, v in obj.items() if k != "@type"}
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
            return str(candidate)
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
# DATA EXTRACTION & MAPPING TO STANDARDIZED SCHEMA
# ============================================================

def get_structured_data(page):
    results = []
    try:
        scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()
        for raw in scripts:
            try:
                results.append(strip_at_type(json.loads(raw)))
            except Exception:
                pass
    except Exception:
        pass

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
    product_url = str(page.url.split("?")[0])
    record = create_standardized_product_record(product_id, product_url)

    content = record["content"]
    entities = record["entities"]
    prod_attrs = record["product_attributes"]
    commercial = record["commercial"]

    structured_blocks = get_structured_data(page)

    for block in structured_blocks:
        def parse_node(obj):
            if isinstance(obj, dict):
                # Title & Description
                if obj.get("name") and not content["title"] and len(str(obj.get("name"))) > 3:
                    title_str = clean_text(obj.get("name"))
                    content["title"] = title_str
                    if entities["products"]:
                        entities["products"][0]["product_name"] = title_str
                if obj.get("sku") and not entities["products"][0]["sku"]:
                    sku_str = str(obj.get("sku"))
                    entities["products"][0]["sku"] = sku_str
                if obj.get("model") and not prod_attrs["model_number"]:
                    model_str = str(obj.get("model"))
                    prod_attrs["model_number"] = model_str
                    entities["products"][0]["model_number"] = model_str
                if obj.get("description") and not content["text"]:
                    content["text"] = clean_text(obj.get("description"))

                # Brand
                brand_obj = obj.get("brand")
                if isinstance(brand_obj, dict):
                    brand_name = brand_obj.get("name")
                    if brand_name and not entities["brands"]:
                        entities["brands"].append({
                            "brand_id": str("BRAND_001"),
                            "brand_name": clean_text(brand_name),
                            "brand_role": str("client")
                        })
                elif isinstance(brand_obj, str) and not entities["brands"]:
                    entities["brands"].append({
                        "brand_id": str("BRAND_001"),
                        "brand_name": clean_text(brand_obj),
                        "brand_role": str("client")
                    })

                # Pricing & Availability (Strict float casting)
                offers = obj.get("offers")
                if offers:
                    offer_list = offers if isinstance(offers, list) else [offers]
                    for offer in offer_list:
                        if isinstance(offer, dict):
                            raw_price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
                            price = parse_numeric_price(raw_price)
                            if price is not None and commercial["price"]["selling_price"]["amount"] is None:
                                f_price = float(price)
                                commercial["price"]["selling_price"]["amount"] = f_price
                                commercial["price"]["listed_price"]["amount"] = f_price
                                commercial["price"]["original_price"]["amount"] = f_price

                            avail = str(offer.get("availability", "")).lower()
                            if "instock" in avail:
                                commercial["availability"]["status"] = str("in_stock")
                            elif "outofstock" in avail:
                                commercial["availability"]["status"] = str("out_of_stock")

                # Media (Images)
                if obj.get("image"):
                    img_list = obj.get("image") if isinstance(obj.get("image"), list) else [obj.get("image")]
                    for im_item in img_list:
                        url = safe_extract_url(im_item)
                        if url and not any(existing["url"] == url for existing in content["media"]["images"]):
                            content["media"]["images"].append({
                                "url": str(url),
                                "type": str("main") if len(content["media"]["images"]) == 0 else str("gallery"),
                                "position": int(len(content["media"]["images"]) + 1)
                            })

                # Media (Videos)
                if obj.get("video") or obj.get("videos"):
                    v_data = obj.get("video") or obj.get("videos")
                    v_list = v_data if isinstance(v_data, list) else [v_data]
                    for item in v_list:
                        if isinstance(item, dict):
                            v_url = item.get("contentUrl") or item.get("url") or item.get("embedUrl")
                            if v_url and not any(existing["url"] == v_url for existing in content["media"]["videos"]):
                                dur = item.get("duration")
                                content["media"]["videos"].append({
                                    "video_id": str(
                                        item.get("identifier") or f"vid_{len(content['media']['videos']) + 1}"),
                                    "url": str(v_url),
                                    "thumbnail_url": str(item.get("thumbnailUrl")) if item.get(
                                        "thumbnailUrl") else None,
                                    "duration_seconds": int(dur) if dur is not None else None
                                })

                for k, v in obj.items():
                    if k not in ("offers", "image", "video", "videos", "brand"):
                        parse_node(v)
            elif isinstance(obj, list):
                for item in obj:
                    parse_node(item)

        parse_node(block)

    # DOM Fallbacks for Title, Price, Media
    try:
        dom_data = page.evaluate("""() => {
            const titleEl = document.querySelector('h1.heading-5, h1[class*="heading"], h1');
            const title = titleEl ? titleEl.innerText : null;

            const priceEl = document.querySelector('.priceView-hero-price span[aria-hidden="true"], [class*="pricing-price"], [class*="priceView-customer-price"]');
            const priceText = priceEl ? priceEl.innerText : null;

            const images = [];
            document.querySelectorAll('img[src*="bbystatic.com"], img[src*="bestbuy.com"]').forEach(img => {
                let src = img.src || img.currentSrc || img.getAttribute('data-src');
                if (src && !src.includes('icon') && !src.includes('logo') && !src.includes('svg')) {
                    src = src.split('?')[0];
                    if (!images.includes(src)) images.push(src);
                }
            });

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

        if not content["title"] and dom_data.get("title"):
            t_str = clean_text(dom_data.get("title"))
            content["title"] = t_str
            if entities["products"]:
                entities["products"][0]["product_name"] = t_str

        if commercial["price"]["selling_price"]["amount"] is None and dom_data.get("priceText"):
            parsed_price = parse_numeric_price(dom_data.get("priceText"))
            if parsed_price is not None:
                f_price = float(parsed_price)
                commercial["price"]["selling_price"]["amount"] = f_price
                commercial["price"]["listed_price"]["amount"] = f_price
                commercial["price"]["original_price"]["amount"] = f_price
                commercial["availability"]["status"] = str("in_stock")

        for img_url in dom_data.get("images", []):
            if not any(existing["url"] == img_url for existing in content["media"]["images"]):
                content["media"]["images"].append({
                    "url": str(img_url),
                    "type": str("main") if len(content["media"]["images"]) == 0 else str("gallery"),
                    "position": int(len(content["media"]["images"]) + 1)
                })

        for vid_item in dom_data.get("videos", []):
            if not any(existing["url"] == vid_item["url"] for existing in content["media"]["videos"]):
                content["media"]["videos"].append({
                    "video_id": str(vid_item["video_id"]),
                    "url": str(vid_item["url"]),
                    "thumbnail_url": None,
                    "duration_seconds": None
                })

    except Exception as e:
        print(f"Error in DOM extraction fallback: {e}")

    # Discount calculations with strict float casting
    lp = commercial["price"]["original_price"]["amount"]
    cp = commercial["price"]["selling_price"]["amount"]
    if lp is not None and cp is not None and lp > cp:
        commercial["price"]["discount_amount"] = float(round(lp - cp, 2))
        commercial["price"]["discount_percentage"] = float(round(((lp - cp) / lp) * 100, 2))
        commercial["promotion"]["is_promotional"] = bool(True)

    return record


def extract_reviews(page, product_id):
    container = create_standardized_reviews_container(product_id)
    summary = container["reviews_summary"]
    items = container["items"]

    structured_blocks = get_structured_data(page)
    for block in structured_blocks:
        candidates = block if isinstance(block, list) else [block]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue

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

            reviews_list = obj.get("review")
            if isinstance(reviews_list, list):
                for idx, r in enumerate(reviews_list):
                    if not isinstance(r, dict):
                        continue

                    rating_dict = r.get("reviewRating")
                    rating_val = 5
                    if isinstance(rating_dict, dict):
                        try:
                            rating_val = int(rating_dict.get("ratingValue") or 5)
                        except (ValueError, TypeError):
                            rating_val = 5

                    str_rating = str(rating_val)
                    if str_rating in summary["star_distribution"]:
                        summary["star_distribution"][str_rating] = int(summary["star_distribution"][str_rating] + 1)

                    author_field = r.get("author")
                    author_name = None
                    if isinstance(author_field, dict):
                        author_name = author_field.get("name")
                    elif isinstance(author_field, str):
                        author_name = author_field

                    review_id = str(r.get("identifier") or f"{product_id}_{idx}")
                    review_record = create_standardized_review_item(product_id, review_id, page.url.split("?")[0])

                    review_record["content"]["title"] = clean_text(r.get("name"))
                    review_record["content"]["text"] = clean_text(
                        r.get("reviewBody") or r.get("description") or r.get("comment"))
                    review_record["metrics"]["rating"]["value"] = int(rating_val)
                    review_record["author"]["display_name"] = clean_text(author_name)

                    items.append(review_record)

    return container


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
                page.wait_for_timeout(4000)

                page.evaluate("window.scrollBy(0, 1000);")
                page.wait_for_timeout(2500)

                product_links = page.evaluate("""(maxProducts) => {
                                    const anchors = Array.from(document.querySelectorAll('a[href*=".p"], a[href*="/product/"], a[href*="/site/"]'));
                                    const seenSkus = new Set();
                                    const uniqueLinks = [];

                                    for (const a of anchors) {
                                        const href = a.href;
                                        if (href && (href.includes('.p') || href.includes('/product/') || href.includes('skuId='))) {
                                            const match = href.match(/(\\d{5,})/);
                                            if (match) {
                                                const sku = match[1];
                                                if (!seenSkus.has(sku)) {
                                                    seenSkus.add(sku);
                                                    uniqueLinks.push(href);
                                                    if (uniqueLinks.length >= maxProducts) {
                                                        break;
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    return uniqueLinks;
                                }""", PRODUCTS_PER_QUERY)

                print(f"Found product links: {product_links}")

                for url in product_links:
                    sku_match = re.search(r'(\d{5,})', url)
                    product_id = sku_match.group(1) if sku_match else None

                    if not product_id:
                        continue

                    print(f"Scraping Standardized Files for Product ID: {product_id} -> {url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(PAGE_WAIT_MS)

                    # File 1: Standardized Product Details Record
                    product_data = extract_product_details(page, product_id)
                    p1_name = os.path.join(OUTPUT_DIR, f"standardized_product_bestbuy_{product_id}.json")
                    with open(p1_name, "w", encoding="utf-8") as f:
                        json.dump(product_data, f, indent=2, ensure_ascii=False)

                    # File 2: Standardized Reviews Record Container
                    reviews_data = extract_reviews(page, product_id)
                    p2_name = os.path.join(OUTPUT_DIR, f"standardized_reviews_bestbuy_{product_id}.json")
                    with open(p2_name, "w", encoding="utf-8") as f:
                        json.dump(reviews_data, f, indent=2, ensure_ascii=False)

                    print(f"Exported successfully -> {p1_name} & {p2_name}")

        finally:
            process.terminate()


if __name__ == "__main__":
    run_scraper()