"""
Configuration settings for Samsung India Laptop Scraper and MongoDB integration.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Base URLs
BASE_URL = "https://www.samsung.com"
LAPTOP_CATALOG_URL = "https://www.samsung.com/in/computers/all-computers/"
COPILOT_CATALOG_URL = "https://www.samsung.com/in/computers/galaxy-book-copilot-plus-pcs/"
IM_SITEMAP_URL = "https://www.samsung.com/in/im-sitemap.xml"

# All Samsung India B2C Product Sitemaps (Covering 100% of Samsung Products)
SITEMAP_URLS = [
    "https://www.samsung.com/in/im-sitemap.xml",      # Smartphones, Tablets, Watches, Rings, Laptops, Audio
    "https://www.samsung.com/in/vd-sitemap.xml",      # Televisions, Soundbars, Monitors, Projectors
    "https://www.samsung.com/in/da-sitemap.xml",      # Refrigerators, Washing Machines, ACs, Microwaves
    "https://www.samsung.com/in/memory-sitemap.xml",  # SSDs, Memory Cards, Storage
]

# Category Catalog Hub URLs on Samsung India
CATEGORY_CATALOG_URLS = {
    "laptops": "https://www.samsung.com/in/computers/all-computers/",
    "computers": "https://www.samsung.com/in/computers/all-computers/",
    "smartphones": "https://www.samsung.com/in/smartphones/all-smartphones/",
    "phones": "https://www.samsung.com/in/smartphones/all-smartphones/",
    "mobile": "https://www.samsung.com/in/smartphones/all-smartphones/",
    "tablets": "https://www.samsung.com/in/tablets/all-tablets/",
    "watches": "https://www.samsung.com/in/watches/all-watches/",
    "wearables": "https://www.samsung.com/in/watches/all-watches/",
    "tvs": "https://www.samsung.com/in/tvs/all-tvs/",
    "televisions": "https://www.samsung.com/in/tvs/all-tvs/",
    "refrigerators": "https://www.samsung.com/in/refrigerators/all-refrigerators/",
    "washers": "https://www.samsung.com/in/washers-and-dryers/all-washers-and-dryers/",
    "washing-machines": "https://www.samsung.com/in/washers-and-dryers/all-washers-and-dryers/",
    "air-conditioners": "https://www.samsung.com/in/air-conditioners/all-air-conditioners/",
    "monitors": "https://www.samsung.com/in/monitors/all-monitors/",
    "audio": "https://www.samsung.com/in/audio-devices/all-audio-devices/",
}

# Supported Retail Platform Configurations
PLATFORM_CONFIGS = {
    "samsung": {
        "name": "Samsung India",
        "aliases": ["samsung", "samsung india", "galaxy", "samsung.com"],
        "domain": "samsung.com",
        "base_url": "https://www.samsung.com",
        "home_url": "https://www.samsung.com/in/",
        "search_url_template": "https://www.samsung.com/in/aisearch/?searchvalue={query}",
        "catalog_urls": CATEGORY_CATALOG_URLS,
        "currency": "INR",
        "marketplace_type": "D2C",
        "region": "IN",
    },
    "croma": {
        "name": "Croma",
        "aliases": ["croma", "tata croma", "croma.com"],
        "domain": "croma.com",
        "base_url": "https://www.croma.com",
        "home_url": "https://www.croma.com/",
        "search_url_template": "https://www.croma.com/searchB?q={query}%3Arelevance&text={query}",
        "product_url_patterns": ["/p/"],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "IN",
    },
    "reliancedigital": {
        "name": "Reliance Digital",
        "aliases": ["reliancedigital", "reliance digital", "reliance", "reliancedigital.in"],
        "domain": "reliancedigital.in",
        "base_url": "https://www.reliancedigital.in",
        "home_url": "https://www.reliancedigital.in/",
        "search_url_template": "https://www.reliancedigital.in/search?q={query}:relevance",
        "product_url_patterns": ["/p/", "/product/"],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "IN",
    },
    "vijaysales": {
        "name": "Vijay Sales",
        "aliases": ["vijaysales", "vijay sales", "vijay", "vijaysales.com"],
        "domain": "vijaysales.com",
        "base_url": "https://www.vijaysales.com",
        "home_url": "https://www.vijaysales.com/",
        "search_url_template": "https://www.vijaysales.com/search/{query}",
        "product_url_patterns": ["/p/"],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "IN",
    },
    "flipkart": {
        "name": "Flipkart",
        "aliases": ["flipkart", "fk", "flipkart.com"],
        "domain": "flipkart.com",
        "base_url": "https://www.flipkart.com",
        "home_url": "https://www.flipkart.com/",
        "search_url_template": "https://www.flipkart.com/search?q={query}",
        "product_url_patterns": ["/p/"],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "IN",
    },
    "amazon": {
        "name": "Amazon India",
        "aliases": ["amazon", "amazon india", "amzn", "amazon.in", "amazon.com"],
        "domain": "amazon.in",
        "base_url": "https://www.amazon.in",
        "home_url": "https://www.amazon.in/",
        "search_url_template": "https://www.amazon.in/s?k={query}",
        "product_url_patterns": ["/dp/", "/gp/product/"],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "IN",
    },
    "alibaba": {
        "name": "Alibaba",
        "aliases": ["alibaba", "ali baba", "alibaba.com"],
        "domain": "alibaba.com",
        "base_url": "https://www.alibaba.com",
        "home_url": "https://www.alibaba.com/",
        "search_url_template": "https://www.alibaba.com/trade/search?searchText={query}&tab=all",
        "product_url_patterns": ["/product-detail/"],
        "currency": "USD",
        "marketplace_type": "B2B",
        "region": "GLOBAL",
    },
    "walmart": {
        "name": "Walmart",
        "aliases": ["walmart", "wall mart", "wallmart", "wal-mart", "walmart.com"],
        "domain": "walmart.com",
        "base_url": "https://www.walmart.com",
        "home_url": "https://www.walmart.com/",
        "search_url_template": "https://www.walmart.com/search?q={query}",
        "product_url_patterns": ["/ip/"],
        "currency": "USD",
        "marketplace_type": "B2C",
        "region": "US",
    },
    "jiomart": {
        "name": "JioMart",
        "aliases": ["jio", "jiomart", "jio mart", "reliance jio", "jiomart.com"],
        "domain": "jiomart.com",
        "base_url": "https://www.jiomart.com",
        "home_url": "https://www.jiomart.com/",
        "search_url_template": "https://www.jiomart.com/search/{query}",
        "product_url_patterns": ["/p/"],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "IN",
    },
    "bestbuy": {
        "name": "Best Buy",
        "aliases": ["bestbuy", "best buy", "best bu y", "bestbuy.com"],
        "domain": "bestbuy.com",
        "base_url": "https://www.bestbuy.com",
        "home_url": "https://www.bestbuy.com/",
        "search_url_template": "https://www.bestbuy.com/site/searchpage.jsp?st={query}&intl=nosplash",
        "product_url_patterns": ["/site/", ".p"],
        "currency": "USD",
        "marketplace_type": "B2C",
        "region": "US",
    },
    "ebay": {
        "name": "eBay",
        "aliases": ["ebay", "ebay.com", "e-bay"],
        "domain": "ebay.com",
        "base_url": "https://www.ebay.com",
        "home_url": "https://www.ebay.com/",
        "search_url_template": "https://www.ebay.com/sch/i.html?_nkw={query}",
        "product_url_patterns": ["/itm/"],
        "currency": "USD",
        "marketplace_type": "B2C",
        "region": "GLOBAL",
    },
    "target": {
        "name": "Target",
        "aliases": ["target", "target.com"],
        "domain": "target.com",
        "base_url": "https://www.target.com",
        "home_url": "https://www.target.com/",
        "search_url_template": "https://www.target.com/s?searchTerm={query}",
        "product_url_patterns": ["/p/"],
        "currency": "USD",
        "marketplace_type": "B2C",
        "region": "US",
    },
    "indiamart": {
        "name": "IndiaMART",
        "aliases": ["indiamart", "india mart", "indiamart.com"],
        "domain": "indiamart.com",
        "base_url": "https://www.indiamart.com",
        "home_url": "https://www.indiamart.com/",
        "search_url_template": "https://dir.indiamart.com/search.mp?ss={query}",
        "product_url_patterns": ["/proddetail/", "/impcat/"],
        "currency": "INR",
        "marketplace_type": "B2B",
        "region": "IN",
    },
    "generic": {
        "name": "Generic Retail Website",
        "aliases": ["generic", "custom", "url", "any"],
        "domain": "",
        "base_url": "",
        "home_url": "",
        "search_url_template": "",
        "product_url_patterns": [],
        "currency": "INR",
        "marketplace_type": "B2C",
        "region": "GLOBAL",
    },
}


def _norm_plat_token(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def search_platforms(query: str = None, include_generic: bool = False) -> list:
    """Search configured retail platforms by key, display name, domain, or alias."""
    raw_q = (query or "").strip().lower()
    compact_q = _norm_plat_token(raw_q)
    matches = []
    for key, cfg in PLATFORM_CONFIGS.items():
        if not include_generic and key == "generic":
            continue
        haystack_items = [key, cfg.get("name", ""), cfg.get("domain", "")] + list(cfg.get("aliases", []))
        if not raw_q or any(
            raw_q in item.lower() or (compact_q and compact_q in _norm_plat_token(item))
            for item in haystack_items
        ):
            matches.append({"key": key, **cfg})
    return matches


def resolve_platform_name(query: str) -> str:
    """Resolve user input like 'wall mart', 'jio', or 'best bu y' to canonical platform key."""
    if not query:
        return query
    raw_q = query.strip().lower()
    if raw_q in ("auto", "all"):
        return raw_q
    compact_q = _norm_plat_token(raw_q)

    # 1. Exact key or alias match
    for key, cfg in PLATFORM_CONFIGS.items():
        candidates = [key, cfg.get("name", "")] + list(cfg.get("aliases", []))
        if any(raw_q == c.lower() or (compact_q and compact_q == _norm_plat_token(c)) for c in candidates):
            return key

    # 2. Substring/partial search match (pick first clear match)
    matches = search_platforms(raw_q, include_generic=True)
    if matches:
        return matches[0]["key"]

    return raw_q

# Bazaarvoice Review API Config
BV_API_URL = "https://api.bazaarvoice.com/data"
BV_PASSKEY = "camJyWiPI5nPG0hRSR4ZtQLx7KGuDKYxZpQzFmqARDFas"

# MongoDB Settings
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "samsung_ecommerce")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "products")
MONGODB_REVIEWS_COLLECTION = os.getenv("MONGODB_REVIEWS_COLLECTION", "reviews")

# File Storage Settings
OUTPUT_JSON_PATH = os.getenv("OUTPUT_JSON_PATH", "output/samsung_products.json")
OUTPUT_REVIEWS_JSON_PATH = os.getenv("OUTPUT_REVIEWS_JSON_PATH", "output/samsung_reviews.json")
OUTPUT_CSV_PATH = os.getenv("OUTPUT_CSV_PATH", "output/samsung_products.csv")
OUTPUT_REVIEWS_CSV_PATH = os.getenv("OUTPUT_REVIEWS_CSV_PATH", "output/samsung_product_reviews.csv")

# Backward Compatibility Paths
OUTPUT_LAPTOPS_JSON_PATH = "output/samsung_laptops.json"
OUTPUT_LAPTOPS_REVIEWS_JSON_PATH = "output/samsung_laptop_reviews.json"
OUTPUT_LAPTOPS_CSV_PATH = "output/samsung_laptops.csv"

# HTTP Request Headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS", "1.0"))

