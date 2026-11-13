"""
Multi-Retail Platform Adapters & Unified Product Extraction Pipeline.
Supports Samsung India plus third-party retail websites (Croma, Reliance Digital,
Vijay Sales, Flipkart, Amazon India, or any Schema.org/OpenGraph product URL).
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import (
    DEFAULT_HEADERS,
    PLATFORM_CONFIGS,
    REQUEST_TIMEOUT,
    resolve_platform_name,
    search_platforms,
)
from models import build_review_v2
from scraper.catalog import get_all_product_urls
from scraper.parser import (
    detect_platform_from_url,
    extract_product_id,
    extract_reviews_v2_from_json_ld,
    format_user_readable_date,
    parse_product_page,
)
from scraper.reviews import fetch_laptop_reviews
logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = tuple(PLATFORM_CONFIGS.keys())

_ANTI_BOT_TITLE_PHRASES = (
    "access denied",
    "attention required",
    "just a moment",
    "robot check",
    "human verification",
    "verify you are human",
    "are you a human",
    "security check",
    "blocked",
)

_ANTI_BOT_BODY_PHRASES = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "verify you are human",
    "please verify you are a human",
    "enable cookies and reload",
    "cf-browser-verification",
    "_cf_chl_opt",
    "challenge-platform",
    "perimeterx",
    "px-captcha",
    "datadome",
    "errors/validatecaptcha",
)


def get_platform_config(platform: str) -> Dict[str, Any]:
    """Return platform configuration dictionary from PLATFORM_CONFIGS."""
    key = (platform or "samsung").strip().lower()
    return PLATFORM_CONFIGS.get(key, PLATFORM_CONFIGS["generic"])


def get_headers_for_url(url: str) -> Dict[str, str]:
    """Return HTTP headers tailored cleanly to the target URL origin."""
    headers = dict(DEFAULT_HEADERS)
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


def detect_anti_bot(status_code: Optional[int], html: str, url: str = "") -> Optional[str]:
    """
    Detect HTTP blocks, WAF walls, or CAPTCHA gates without ever bypassing them.
    Returns a descriptive block reason if gated, else None.
    """
    domain = urlparse(url).netloc or "target website"
    html_str = html or ""
    lower_html = html_str.lower()

    if status_code in (403, 429):
        return f"HTTP {status_code} access restriction / rate limit from {domain}."

    soup = BeautifulSoup(html_str[:120000], "html.parser") if html_str else None
    title_text = soup.title.get_text(" ", strip=True).lower() if soup and soup.title else ""

    for phrase in _ANTI_BOT_TITLE_PHRASES:
        if phrase in title_text:
            return f"Blocked by anti-bot / verification gate ('{phrase}') on {domain}."

    has_product_markup = (
        '"@type":"product"' in lower_html.replace(" ", "")
        or 'property="og:title"' in lower_html
        or "digitaldata" in lower_html
    )

    if status_code == 503 and not has_product_markup:
        return f"HTTP 503 service unavailable / bot challenge from {domain}."

    if not has_product_markup or len(html_str) < 60000:
        for marker in _ANTI_BOT_BODY_PHRASES:
            if marker in lower_html:
                return f"CAPTCHA / anti-bot gate detected ('{marker}') on {domain}."

    return None


def build_search_url(platform: str, query: str) -> Optional[str]:
    """Build a platform search URL from its template in PLATFORM_CONFIGS."""
    cfg = get_platform_config(platform)
    template = cfg.get("search_url_template") or ""
    if not template or not query:
        return None
    if "{query}" not in template:
        return template
    # Path-based templates (e.g. Vijay Sales) prefer percent encoding; query params use plus encoding
    encoded = quote(query.strip()) if "/search/{query}" in template else quote_plus(query.strip())
    return template.format(query=encoded)


def _is_candidate_product_url(url: str, platform: str, domain: str) -> bool:
    """Check whether a URL matches the expected domain and PDP URL shape."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    netloc_lower = parsed.netloc.lower()
    if domain and domain not in netloc_lower:
        return False

    path_lower = parsed.path.lower()
    if any(
        skip in path_lower
        for skip in (
            "/cart",
            "/checkout",
            "/login",
            "/account",
            "/wishlist",
            "/help",
            "/support",
            "/privacy",
            "/terms",
            ".jpg",
            ".png",
            ".css",
            ".js",
        )
    ):
        return False

    cfg = get_platform_config(platform)
    if platform == "bestbuy":
        return "/site/" in path_lower and (path_lower.endswith(".p") or "skuid=" in url.lower())

    patterns = cfg.get("product_url_patterns") or []
    if patterns:
        return any(pat in path_lower for pat in patterns)

    if platform == "samsung":
        return any(
            seg in path_lower
            for seg in (
                "/smartphones/",
                "/computers/",
                "/tablets/",
                "/watches/",
                "/tvs/",
                "/refrigerators/",
                "/washers",
                "/air-conditioners/",
                "/monitors/",
                "/audio",
            )
        ) and not path_lower.endswith(("-all/", "/all-smartphones/", "/all-computers/"))

    return any(
        pat in path_lower
        for pat in ("/p/", "/dp/", "/ip/", "/itm/", "/proddetail/", "/impcat/", "/product/", "/product-detail/", "/pd/", "/buy/")
    )


def extract_search_result_urls(
    html: str,
    search_url: str,
    platform: str,
    limit: int = 20,
) -> List[str]:
    """Extract candidate product detail URLs from search results or catalog HTML."""
    cfg = get_platform_config(platform)
    domain = cfg.get("domain", "")
    soup = BeautifulSoup(html, "html.parser")

    discovered: List[str] = []
    seen: Set[str] = set()

    def _record(raw_href: str) -> None:
        if not raw_href or len(discovered) >= limit:
            return
        full_url = urljoin(search_url, raw_href.strip()).split("#")[0]
        if platform == "amazon" and "/dp/" in full_url:
            dp_match = re.search(r"(https?://[^/]+/[^?]*?/dp/[A-Z0-9]{10}|https?://[^/]+/dp/[A-Z0-9]{10})", full_url)
            if dp_match:
                full_url = dp_match.group(1)
        elif platform in ("walmart", "alibaba", "jiomart", "croma", "vijaysales", "reliancedigital"):
            full_url = full_url.split("?")[0]
        if full_url not in seen and _is_candidate_product_url(full_url, platform, domain):
            seen.add(full_url)
            discovered.append(full_url)

    # 1. JSON-LD ItemList URLs when provided in SSR search pages
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_json = (script.string or script.get_text() or "").strip()
        if "ItemList" not in raw_json:
            continue
        for url_match in re.findall(r'"url"\s*:\s*"([^"]+)"', raw_json):
            _record(url_match)

    # 2. Anchor href discovery (prefer main/search content and skip header/nav/footer chrome)
    content_roots = soup.select("main, [role='main'], #content, .aisearch, .search-results, .result-list")
    seen_anchors: Set[int] = set()
    anchors_to_scan = []
    for root in content_roots:
        for a in root.find_all("a", href=True):
            if not a.find_parent(["header", "nav", "footer"]) and id(a) not in seen_anchors:
                seen_anchors.add(id(a))
                anchors_to_scan.append(a)
    if not anchors_to_scan:
        anchors_to_scan = [
            a for a in soup.find_all("a", href=True)
            if not a.find_parent(["header", "nav", "footer"])
        ] or soup.find_all("a", href=True)

    for anchor in anchors_to_scan:
        _record(anchor["href"])
        if len(discovered) >= limit:
            break

    return discovered


def _rank_urls_by_query(urls: List[str], search_query: str) -> List[str]:
    """Rank URLs so slugs containing search tokens appear first."""
    tokens = [tok for tok in re.split(r"\s+", search_query.strip().lower()) if len(tok) > 1]
    if not tokens:
        return urls

    scored: List[Tuple[int, int, str]] = []
    for idx, url in enumerate(urls):
        slug = urlparse(url).path.lower()
        hits = sum(1 for tok in tokens if tok in slug)
        if hits > 0:
            scored.append((-hits, idx, url))

    if scored:
        scored.sort(key=lambda entry: (entry[0], entry[1]))
        matched_set = {entry[2] for entry in scored}
        # Put slug-matched URLs first; keep bounded unmatched candidates only if few matches exist
        tail = [u for u in urls if u not in matched_set] if len(scored) < 5 else []
        return [entry[2] for entry in scored] + tail[:15]

    return urls


def _discover_from_sitemap(
    base_url: str,
    platform: str,
    domain: str,
    search_query: Optional[str],
    limit: int,
    session: requests.Session,
) -> List[str]:
    """Best-effort XML sitemap URL discovery for third-party retail websites."""
    if not base_url:
        return []

    sitemap_candidates = [f"{base_url.rstrip('/')}/sitemap.xml"]
    visited: Set[str] = set()
    product_urls: List[str] = []

    while sitemap_candidates and len(product_urls) < limit * 4 and len(visited) < 5:
        sm_url = sitemap_candidates.pop(0)
        if sm_url in visited:
            continue
        visited.add(sm_url)

        try:
            resp = session.get(sm_url, headers=get_headers_for_url(sm_url), timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200 or not resp.text.strip().startswith("<"):
                continue
            root = ET.fromstring(resp.text)
            for loc in root.iter():
                if loc.tag.endswith("loc") and loc.text:
                    loc_url = loc.text.strip()
                    if loc_url.endswith(".xml") and len(sitemap_candidates) < 6:
                        if any(k in loc_url.lower() for k in ("product", "pdp", "catalog", "item")) or not sitemap_candidates:
                            sitemap_candidates.append(loc_url)
                    elif _is_candidate_product_url(loc_url, platform, domain):
                        product_urls.append(loc_url)
        except Exception:
            continue

    if search_query:
        product_urls = _rank_urls_by_query(product_urls, search_query)

    return product_urls[:limit]


def _fetch_search_html_playwright(target_url: str) -> Tuple[Optional[int], str]:
    """Render a JS-heavy retail search page using headless Playwright Firefox."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        used_firefox = False
        try:
            browser = p.firefox.launch(headless=True)
            used_firefox = True
        except Exception:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])

        context_kwargs = {"viewport": {"width": 1920, "height": 1080}}
        if not used_firefox:
            context_kwargs["user_agent"] = DEFAULT_HEADERS["User-Agent"]
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        try:
            resp = page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            page.evaluate("window.scrollBy(0, 900);")
            page.wait_for_timeout(1500)
            status_code = resp.status if resp else None
            return status_code, page.content()
        finally:
            context.close()
            browser.close()


def discover_urls_for_platform(
    platform: str = "samsung",
    category: Optional[str] = None,
    search_query: Optional[str] = None,
    limit: int = 20,
    session: Optional[requests.Session] = None,
    use_playwright: bool = False,
) -> Tuple[List[str], Optional[str]]:
    """
    Discover candidate product URLs for a given retail platform.
    Returns (urls, warning_or_block_reason).
    """
    plat = resolve_platform_name((platform or "samsung").strip().lower())
    if plat not in PLATFORM_CONFIGS and plat != "auto":
        return [], f"Unsupported platform '{platform}'. Supported: {', '.join(SUPPORTED_PLATFORMS)}."

    close_session = False
    if session is None:
        session = requests.Session()
        close_session = True

    try:
        if plat == "samsung":
            urls = get_all_product_urls(category=category, session=session)
            if search_query:
                urls = _rank_urls_by_query(urls, search_query)
            return urls, None

        if plat == "generic":
            return [], "Generic platform cannot search without a domain; pass direct --url or --urls."

        cfg = get_platform_config(plat)
        domain = cfg.get("domain", "")
        base_url = cfg.get("base_url", "")
        target_url = build_search_url(plat, search_query) if search_query else cfg.get("home_url")

        discovered: List[str] = []
        warning: Optional[str] = None

        if target_url:
            logger.info(f"🔎 Searching {cfg.get('name', plat)} ({plat}): {target_url}")
            if use_playwright:
                try:
                    status_code, rendered_html = _fetch_search_html_playwright(target_url)
                    block_reason = detect_anti_bot(status_code, rendered_html, target_url)
                    if block_reason:
                        return [], block_reason
                    discovered = extract_search_result_urls(rendered_html, target_url, plat, limit=limit)
                except Exception as exc:
                    warning = f"Playwright search failed on {cfg.get('name', plat)}: {exc}"
            else:
                try:
                    resp = session.get(target_url, headers=get_headers_for_url(target_url), timeout=REQUEST_TIMEOUT)
                    block_reason = detect_anti_bot(resp.status_code, resp.text, target_url)
                    if block_reason:
                        return [], block_reason
                    resp.raise_for_status()
                    discovered = extract_search_result_urls(resp.text, target_url, plat, limit=limit)
                except requests.RequestException as exc:
                    warning = f"Search request failed on {cfg.get('name', plat)}: {exc}"

        if not discovered:
            sitemap_urls = _discover_from_sitemap(
                base_url=base_url,
                platform=plat,
                domain=domain,
                search_query=search_query,
                limit=limit,
                session=session,
            )
            discovered.extend(sitemap_urls)

        if not discovered and not warning:
            warning = (
                f"No static product links extracted from {cfg.get('name', plat)}. "
                "The site may render search results via JavaScript (try --playwright or --visual, "
                "or pass a direct product --url / --urls)."
            )

        return discovered[:limit], warning
    finally:
        if close_session:
            session.close()


def scrape_product_and_reviews(
    url: str,
    platform: str = "auto",
    session: Optional[requests.Session] = None,
    html_override: Optional[str] = None,
    days_cutoff: Optional[int] = 365,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """
    Scrape any Samsung or third-party retail product URL into separate Schema v2.0.0
    documents: (product_v2, review_v2, error_or_block_reason).
    """
    close_session = False
    sess = session
    if sess is None:
        sess = requests.Session()
        close_session = True

    try:
        html = html_override
        if html is None:
            resp = sess.get(url, headers=get_headers_for_url(url), timeout=REQUEST_TIMEOUT)
            block_reason = detect_anti_bot(resp.status_code, resp.text, url)
            if block_reason:
                return None, None, block_reason
            resp.raise_for_status()
            html = resp.text
        else:
            block_reason = detect_anti_bot(None, html, url)
            if block_reason:
                return None, None, block_reason

        detected = detect_platform_from_url(url)
        requested = resolve_platform_name((platform or "auto").strip().lower())
        effective_platform = detected if requested in ("", "auto", "generic") else requested

        parsed = parse_product_page(
            html,
            url,
            first_reviewed_date=None,
            platform=effective_platform,
        )
        v2_product = parsed["v2_product"]
        pid = parsed.get("product_id") or extract_product_id(html, url)
        parent_id = parsed.get("parent_product_id")
        seller_id = parsed.get("seller_id")

        review_v2: Optional[Dict[str, Any]] = None
        earliest_rev_date: Optional[str] = None

        if effective_platform == "samsung" and pid:
            review_v2, earliest_rev_date = fetch_laptop_reviews(
                product_id=pid,
                session=sess,
                days_cutoff=days_cutoff,
                seller_id=seller_id,
                parent_product_id=parent_id,
            )
            if earliest_rev_date and not v2_product["product"]["core"].get("introduced_date"):
                v2_product["product"]["core"]["introduced_date"] = format_user_readable_date(earliest_rev_date)

        # Fallback or primary JSON-LD review extraction for non-Samsung retail platforms
        if not review_v2:
            review_v2, ld_earliest = extract_reviews_v2_from_json_ld(
                parsed.get("json_ld_products") or [],
                platform=parsed["platform"],
                platform_product_id=pid,
                parent_product_id=parent_id,
                seller_id=seller_id,
                days_cutoff=days_cutoff,
                fallback_rating=parsed.get("fallback_rating"),
            )
            if ld_earliest and not v2_product["product"]["core"].get("introduced_date"):
                v2_product["product"]["core"]["introduced_date"] = format_user_readable_date(ld_earliest)

        if not review_v2:
            review_v2 = build_review_v2(
                platform=parsed["platform"],
                platform_product_id=pid,
                parent_product_id=parent_id,
                seller_id=seller_id,
            )

        return v2_product, review_v2, None

    except requests.RequestException as exc:
        return None, None, f"HTTP request error: {exc}"
    except Exception as exc:
        return None, None, f"Extraction error: {exc}"
    finally:
        if close_session:
            sess.close()


def scrape_single_product(
    url: str,
    platform: str = "auto",
    session: Optional[requests.Session] = None,
    html_override: Optional[str] = None,
    days_cutoff: Optional[int] = 365,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Backward-compatible wrapper returning (product_v2_dict, error_or_block_reason).
    """
    product_v2, _, err = scrape_product_and_reviews(
        url=url,
        platform=platform,
        session=session,
        html_override=html_override,
        days_cutoff=days_cutoff,
    )
    return product_v2, err
