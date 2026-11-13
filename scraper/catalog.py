"""
Catalog discovery module.
Discovers all product URLs across Samsung India for ANY product category
(smartphones, laptops, televisions, tablets, refrigerators, monitors, audio, etc.)
using Samsung's official B2C sitemaps and dynamic catalog pages.
"""

import logging
import re
import json
import requests
from typing import List, Set, Optional
from urllib.parse import urljoin

from config import (
    BASE_URL,
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    SITEMAP_URLS,
    CATEGORY_CATALOG_URLS,
    IM_SITEMAP_URL,
    LAPTOP_CATALOG_URL,
    COPILOT_CATALOG_URL
)

logger = logging.getLogger(__name__)

# Exclude non-product landing, guide, comparison, and accessory hub pages
EXCLUDE_URL_PATTERNS = [
    "/buy/", "/compare/", "buying-guide", "/support/", "/accessories/",
    "accessory", "/campaign/", "help-me-choose", "all-smartphones", "all-computers",
    "all-tvs", "all-refrigerators", "all-audio-devices", "all-tablets",
    "all-watches", "all-monitors", "all-washers-and-dryers", "all-air-conditioners",
    "/b2b/", "/business/", "terms", "faq", "overview",
    "switch-to-galaxy", "ampyourawesome", "ai-energy-saving"
]


def _matches_samsung_category(link_lower: str, category_filter: Optional[str]) -> bool:
    """Shared category filter matching Samsung India URL paths cleanly."""
    cat_term = category_filter.strip().lower() if category_filter else None
    if not cat_term or cat_term == "all":
        return True
    if cat_term in ["laptop", "laptops", "computers", "galaxy-book"]:
        return "galaxy-book" in link_lower or "/computers/" in link_lower
    if cat_term in ["phone", "phones", "smartphones", "mobile"]:
        return "/smartphones/" in link_lower
    if cat_term in ["tablet", "tablets", "galaxy-tab"]:
        return "/tablets/" in link_lower
    if cat_term in ["tv", "tvs", "televisions"]:
        return "/tvs/" in link_lower or "/lifestyle-tvs/" in link_lower
    if cat_term in ["fridge", "fridges", "refrigerators"]:
        return "/refrigerators/" in link_lower
    if cat_term in ["watch", "watches", "wearables"]:
        return "/watches/" in link_lower or "/rings/" in link_lower
    return cat_term in link_lower

# Regex identifying clean product detail page model suffixes:
# e.g., -np960xjg-kg4in, -sm-s928bzkgins, -qa65qn900d, -hw-ls60d-xl, -rt34...
PRODUCT_MODEL_SUFFIX_REGEX = re.compile(
    r'-([a-z]{1,4}\d{1,4}[a-z0-9\-]+|[a-z0-9]{3,}-[a-z0-9]{3,})/?$',
    re.I
)


def fetch_page_content(url: str, session: requests.Session) -> str:
    """Fetch content from a URL safely."""
    try:
        resp = session.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return ""


def extract_product_urls_from_sitemap(sitemap_xml: str, category_filter: Optional[str] = None) -> Set[str]:
    """
    Extracts individual product detail URLs from a Samsung sitemap XML string.
    Optionally filters by product category.
    """
    urls: Set[str] = set()
    locs = re.findall(r'<loc>(https://www\.samsung\.com/in/[^\s<>]+)</loc>', sitemap_xml)

    for link in locs:
        l_lower = link.lower()
        if any(skip in l_lower for skip in EXCLUDE_URL_PATTERNS):
            continue
        if not _matches_samsung_category(l_lower, category_filter):
            continue

        parts = [p for p in link.replace("https://www.samsung.com/in/", "").strip("/").split("/") if p]
        if len(parts) >= 2 and PRODUCT_MODEL_SUFFIX_REGEX.search(parts[-1]):
            clean_url = link.rstrip("/") + "/"
            urls.add(clean_url)

    return urls


def extract_product_urls_from_html(html: str, category_filter: Optional[str] = None) -> Set[str]:
    """
    Extracts individual product detail URLs from a catalog page HTML,
    respecting category_filter so global navbar links are not mixed in.
    """
    urls: Set[str] = set()

    # 1. Check Schema.org ItemList JSON-LD
    json_ld_matches = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
        html,
        re.I
    )
    for block in json_ld_matches:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                for elem in data.get("itemListElement", []):
                    item = elem.get("item", {})
                    url = item.get("url") or item.get("@id")
                    if (
                        url
                        and not any(skip in url.lower() for skip in EXCLUDE_URL_PATTERNS)
                        and _matches_samsung_category(url.lower(), category_filter)
                    ):
                        urls.add(url.strip().rstrip("/") + "/")
        except Exception:
            pass

    # 2. Extract model-level product anchor links
    anchor_matches = re.findall(
        r'href=["\'](/in/(?:smartphones|computers|tablets|tvs|watches|refrigerators|monitors|audio-devices)/[^"\'#\?]+)["\']',
        html
    )
    for link in anchor_matches:
        if any(skip in link.lower() for skip in EXCLUDE_URL_PATTERNS):
            continue
        full_url = urljoin(BASE_URL, link).rstrip("/") + "/"
        if not _matches_samsung_category(full_url.lower(), category_filter):
            continue
        parts = [p for p in full_url.replace("https://www.samsung.com/in/", "").strip("/").split("/") if p]
        if len(parts) >= 2 and PRODUCT_MODEL_SUFFIX_REGEX.search(parts[-1]):
            urls.add(full_url)

    return urls


def get_all_product_urls(category: Optional[str] = None, session: requests.Session = None) -> List[str]:
    """
    Discovers product URLs across Samsung India.
    Supports any product category: 'smartphones', 'laptops', 'tvs', 'tablets', 'refrigerators', 'all', etc.
    """
    close_session = False
    if session is None:
        session = requests.Session()
        close_session = True

    discovered_urls: Set[str] = set()
    cat_query = (category or "").strip().lower()

    # Determine which sitemaps to scan based on category
    target_sitemaps = SITEMAP_URLS
    if cat_query in ["laptop", "laptops", "computers", "smartphones", "phones", "tablets", "watches"]:
        target_sitemaps = [IM_SITEMAP_URL]
    elif cat_query in ["tv", "tvs", "televisions", "monitors", "audio", "soundbars"]:
        target_sitemaps = ["https://www.samsung.com/in/vd-sitemap.xml"]
    elif cat_query in ["refrigerators", "washers", "air-conditioners", "appliances"]:
        target_sitemaps = ["https://www.samsung.com/in/da-sitemap.xml"]

    logger.info(f"🔎 Scanning Samsung product sitemaps for category: '{cat_query or 'ALL PRODUCTS'}'...")
    for sm_url in target_sitemaps:
        sitemap_xml = fetch_page_content(sm_url, session)
        if sitemap_xml:
            found = extract_product_urls_from_sitemap(sitemap_xml, category_filter=cat_query)
            logger.info(f"  ↳ Discovered {len(found)} product(s) from {sm_url}")
            discovered_urls.update(found)

    # Optional augmentation with category catalog hub
    catalog_hub = CATEGORY_CATALOG_URLS.get(cat_query)
    if catalog_hub:
        logger.info(f"Scanning category catalog page: {catalog_hub}")
        html = fetch_page_content(catalog_hub, session)
        if html:
            found = extract_product_urls_from_html(html, category_filter=cat_query)
            discovered_urls.update(found)

    if close_session:
        session.close()

    result = sorted(list(discovered_urls))
    logger.info(f"✓ Total unique product URLs discovered: {len(result)}")
    return result


def get_all_laptop_urls(session: requests.Session = None) -> List[str]:
    """Backward compatibility alias for laptop discovery."""
    return get_all_product_urls(category="laptops", session=session)
