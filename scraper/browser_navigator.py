"""
Automated Visual Browser Navigator for Samsung India Laptops.
Performs real end-to-end browser navigation:
1. Opens https://www.samsung.com/in/
2. Clicks the search icon on Samsung's navigation bar
3. Types search query (e.g. 'laptops', 'galaxy', 'hp laptops', 'dell') and executes search
4. Checks brand availability: returns empty list [] for non-Samsung brands
5. Expands catalog pagination by clicking 'View more' buttons
6. Script navigates through each and every laptop in the list ONE BY ONE
7. Scrolls smoothly through details, specs, and reviews
8. Extracts past 1-year data and syncs live to MongoDB
"""

import re
import sys
import logging
from typing import Dict, Any, Optional, List
from playwright.sync_api import sync_playwright

from config import CATEGORY_CATALOG_URLS, OUTPUT_JSON_PATH, PLATFORM_CONFIGS, resolve_platform_name
from scraper.catalog import get_all_product_urls
from scraper.filters import (
    ProductFilter,
    filter_and_sort_products,
    format_price_str,
    matches_filter,
)
from models import extract_flat_summary, make_review_key
from scraper.platforms import (
    build_search_url,
    detect_anti_bot,
    detect_platform_from_url,
    discover_urls_for_platform,
    extract_search_result_urls,
    get_platform_config,
    scrape_product_and_reviews,
)
from database import MongoManager, save_laptops_to_json
from scraper.playwright_scraper import expand_pdp_sections

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("browser_navigator")

# Non-Samsung brands and product lines that are not sold on Samsung.com
NON_SAMSUNG_BRANDS = [
    # Apple ecosystem
    "apple", "macbook", "mac", "iphone", "ipad", "airpods", "imac", "apple watch",
    # HP
    "hp", "hewlett packard", "pavilion", "spectre", "envy", "omen", "victus",
    # Dell
    "dell", "delll", "alienware", "inspiron", "xps", "vostro", "latitude",
    # Lenovo
    "lenovo", "thinkpad", "ideapad", "legion", "yoga", "loq",
    # Other PC & Mobile Brands
    "acer", "asus", "rog", "tuf", "zenbook", "vivobook", "toshiba", "msi",
    "microsoft", "surface", "sony", "vaio", "bravia", "razer", "lg", "gram",
    "huawei", "xiaomi", "redmi", "poco", "oneplus", "oppo", "vivo", "realme",
    "motorola", "moto", "pixel", "google pixel", "nokia", "honor"
]


def is_brand_available_on_samsung(query: Optional[str]) -> bool:
    """
    Checks if the searched brand is sold on Samsung.com.
    Samsung only manufactures and sells Samsung Galaxy and consumer electronic products.
    Third-party brands like HP, Dell, Lenovo, Apple, LG are not sold on Samsung.
    """
    if not query:
        return True

    q_clean = query.strip().lower()
    words = set(re.findall(r'[a-zA-Z0-9]+', q_clean))

    for brand in NON_SAMSUNG_BRANDS:
        b_lower = brand.lower()
        if b_lower in words or b_lower == q_clean or q_clean.startswith(b_lower + " ") or f" {b_lower} " in f" {q_clean} ":
            return False

    return True


def resolve_category_from_query(query: str) -> Optional[str]:
    """Detects target product category from user search term."""
    q = (query or "").strip().lower()
    if any(k in q for k in ["laptop", "laptops", "galaxy-book", "galaxy book", "copilot"]):
        return "laptops"
    if any(k in q for k in ["smartphone", "smartphones", "phone", "phones", "mobile", "galaxy s", "galaxy a", "fold", "flip"]):
        return "smartphones"
    if any(k in q for k in ["tablet", "tablets", "galaxy tab", "tab"]):
        return "tablets"
    if any(k in q for k in ["tv", "tvs", "television", "televisions", "neo qled", "qled", "oled", "crystal uhd"]):
        return "tvs"
    if any(k in q for k in ["watch", "watches", "galaxy watch", "ring", "galaxy ring"]):
        return "watches"
    if any(k in q for k in ["fridge", "fridges", "refrigerator", "refrigerators"]):
        return "refrigerators"
    if any(k in q for k in ["washer", "washers", "washing machine", "dryer"]):
        return "washers"
    if any(k in q for k in ["ac", "air conditioner", "air conditioners"]):
        return "air-conditioners"
    if any(k in q for k in ["monitor", "monitors", "odyssey", "viewfinity"]):
        return "monitors"
    if any(k in q for k in ["audio", "soundbar", "soundbars", "sound tower", "music frame", "earbuds", "buds"]):
        return "audio"
    return None


def _launch_browser(p, headless: bool, browser_name: str = "auto"):
    """Launches the requested browser (auto, firefox, edge, chrome, chromium) with fallback."""
    req = (browser_name or "auto").strip().lower()
    order_map = {
        "firefox": ["firefox", "edge", "chrome", "chromium"],
        "edge": ["edge", "firefox", "chrome", "chromium"],
        "msedge": ["edge", "firefox", "chrome", "chromium"],
        "chrome": ["chrome", "edge", "firefox", "chromium"],
        "chromium": ["chromium", "chrome", "edge", "firefox"],
    }
    candidates = order_map.get(req, ["firefox", "edge", "chrome", "chromium"])
    last_err = None
    slow = 300 if not headless else 0

    for engine in candidates:
        try:
            if engine == "firefox":
                logger.info("Starting browser engine (Firefox)...")
                browser = p.firefox.launch(headless=headless, slow_mo=slow)
                logger.info("✓ Firefox launched successfully.")
                return browser, True
            if engine == "edge":
                logger.info("Starting browser engine (Microsoft Edge)...")
                browser = p.chromium.launch(
                    channel="msedge",
                    headless=headless,
                    slow_mo=slow,
                    args=["--start-maximized", "--no-sandbox"],
                )
                logger.info("✓ Microsoft Edge launched successfully.")
                return browser, False
            if engine == "chrome":
                logger.info("Starting browser engine (Google Chrome)...")
                browser = p.chromium.launch(
                    channel="chrome",
                    headless=headless,
                    slow_mo=slow,
                    args=["--start-maximized", "--no-sandbox"],
                )
                logger.info("✓ Google Chrome launched successfully.")
                return browser, False
            logger.info("Starting browser engine (Chromium)...")
            browser = p.chromium.launch(
                headless=headless,
                slow_mo=slow,
                ignore_default_args=["--disable-extensions"],
                args=["--start-maximized", "--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
            )
            logger.info("✓ Chromium launched successfully.")
            return browser, False
        except Exception as exc:
            last_err = exc
            logger.warning(f"Browser engine '{engine}' unavailable ({exc}); trying fallback...")

    logger.error(f"All browsers failed to launch: {last_err}")
    raise last_err


def run_automated_navigation(
    headless: bool = False,
    search_query: Optional[str] = "laptops",
    target_laptop_url: Optional[str] = None,
    limit: Optional[int] = None,
    past_year_only: bool = True,
    save_to_mongo: bool = True,
    mongo_uri: Optional[str] = None,
    output_file: Optional[str] = OUTPUT_JSON_PATH,
    platform: str = "samsung",
    product_filter: Optional[ProductFilter] = None,
    browser_name: str = "auto",
) -> List[Dict[str, Any]]:
    """
    End-to-End Automated Browser Navigation Workflow:
    - Supports Samsung India plus third-party retail websites via Playwright Firefox/Edge
    - Handles visual search, catalog/pagination discovery, per-product scrolling & extraction
    - Enforces ProductFilter criteria (date range, price range, rating, brand, stock) and syncs DB/JSON
    """
    plat = resolve_platform_name((platform or "samsung").strip().lower())
    if target_laptop_url and plat in ("", "all", "auto", "generic"):
        plat = detect_platform_from_url(target_laptop_url)
    elif not plat or plat in ("all", "auto"):
        plat = "samsung"

    plat_cfg = get_platform_config(plat)
    query_str = (search_query or "laptops").strip()
    active_filter = product_filter or ProductFilter(
        platform=None if plat in ("auto", "samsung", "generic") else plat,
        days=365,
        all_time=not past_year_only,
    )
    effective_days = None if active_filter.all_time else active_filter.days

    logger.info("=" * 85)
    logger.info(f"🚀 STARTING AUTOMATED SCRIPT NAVIGATION ON {plat_cfg.get('name', plat).upper()}")
    logger.info(
        f"🔎 Platform: '{plat}' | Query: '{query_str}' | "
        f"Date Filter: {'All Time' if active_filter.all_time else f'Last {active_filter.days} days'}"
    )
    logger.info("=" * 85)

    # Non-Samsung brand restriction ONLY applies when scraping Samsung.com
    is_valid_samsung_brand = is_brand_available_on_samsung(query_str) if plat == "samsung" else True

    results_list: List[Dict[str, Any]] = []
    reviews_by_key: Dict[Any, Dict[str, Any]] = {}

    with sync_playwright() as p:
        logger.info(f"Launching browser (headless={headless}, requested={browser_name})...")
        browser, used_firefox = _launch_browser(p, headless=headless, browser_name=browser_name)

        ctx_kwargs: Dict[str, Any] = {
            "no_viewport": True if not headless else False,
            "viewport": {"width": 1920, "height": 1080} if headless else None,
        }
        if not used_firefox:
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        def _dismiss_common_overlays() -> None:
            for sel in (
                "#truste-consent-button",
                "button:has-text('Accept All')",
                "button:has-text('Accept Cookies')",
                "button[aria-label='Close']",
                "button._2KpZ6l._2doB4z",
            ):
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click(timeout=1500)
                        page.wait_for_timeout(400)
                        break
                except Exception:
                    pass

        try:
            discovered_urls: List[str] = []
            candidate_cap = max((limit or 10) * 3, 15)

            if target_laptop_url:
                discovered_urls = [target_laptop_url]
            elif plat != "samsung":
                # Third-party / generic retail visual search navigation
                start_url = build_search_url(plat, query_str) or plat_cfg.get("home_url")
                if not start_url:
                    logger.warning(f"⚠️ No search or homepage URL configured for platform '{plat}'. Pass --url.")
                    return []

                logger.info(f"🌐 [1/4] Opening {plat_cfg.get('name', plat)}: {start_url}")
                page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                _dismiss_common_overlays()

                # If landing on homepage (no search template), attempt search bar fill + Enter
                if not build_search_url(plat, query_str):
                    for s_sel in (
                        "input[type='search']:visible",
                        "input[name='q']:visible",
                        "input[name='k']:visible",
                        "input[name='search']:visible",
                        "input[placeholder*='Search' i]:visible",
                    ):
                        try:
                            inp = page.query_selector(s_sel)
                            if inp:
                                inp.fill(query_str)
                                page.keyboard.press("Enter")
                                page.wait_for_timeout(3000)
                                break
                        except Exception:
                            pass

                nav_block = detect_anti_bot(None, page.content(), page.url)
                if nav_block:
                    logger.warning(f"⛔ {nav_block}")
                    page.wait_for_timeout(2000)
                    return []

                for _ in range(3):
                    page.evaluate("window.scrollBy({top: 800, behavior: 'smooth'});")
                    page.wait_for_timeout(1200)

                discovered_urls = extract_search_result_urls(page.content(), page.url, plat, limit=candidate_cap)
                if not discovered_urls:
                    sitemap_urls, warn = discover_urls_for_platform(
                        platform=plat,
                        category=active_filter.category,
                        search_query=query_str,
                        limit=candidate_cap,
                        use_playwright=False,
                    )
                    if warn:
                        logger.warning(warn)
                    discovered_urls = sitemap_urls
            else:
                # ------------------------------------------------------------------
                # Samsung India Visual Search & Catalog Discovery
                # ------------------------------------------------------------------
                logger.info("🌐 [1/5] Opening Samsung India homepage (https://www.samsung.com/in/)...")
                page.goto("https://www.samsung.com/in/", wait_until="domcontentloaded", timeout=45000)
                logger.info("✓ Samsung India homepage loaded.")
                page.wait_for_timeout(2000)
                _dismiss_common_overlays()

                logger.info("🔍 [2/5] Interacting with search bar on Samsung.com...")
                search_opened = False
                for btn_sel in (
                    "button.nv00-gnb-v5__utility-search:visible",
                    "button.gnb__search-btn-js:visible",
                    "button[aria-label='Search']:visible",
                    "button[an-la='search']:visible",
                ):
                    try:
                        search_btn = page.query_selector(btn_sel)
                        if search_btn:
                            search_btn.click(timeout=3000)
                            page.wait_for_timeout(1000)
                            search_opened = True
                            break
                    except Exception as e_btn:
                        logger.debug(f"Search button click attempt ({btn_sel}): {e_btn}")

                search_input = page.query_selector(
                    "input#gnb-search-keyword, input.srd19-gnb-search__input, input[type='search']:visible"
                )

                if search_input and search_opened:
                    logger.info(f"⌨️ Typing '{query_str}' into search input...")
                    search_input.fill(query_str)
                    page.wait_for_timeout(800)
                    logger.info("↵ Submitting search...")
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3000)
                else:
                    search_url = f"https://www.samsung.com/in/aisearch/?searchvalue={query_str}"
                    logger.info(f"Navigating directly to search URL: {search_url}")
                    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(3000)

                logger.info(f"✓ Search executed! Current URL: {page.url}")

                if not is_valid_samsung_brand:
                    logger.warning(f"⚠️ Brand '{query_str}' is NOT sold by Samsung.")
                    logger.info("ℹ️ Samsung only manufactures and sells Samsung Galaxy & consumer electronics.")
                    logger.info("=" * 85)
                    logger.info(f"📋 RESULT FOR '{query_str}': [] (Empty List)")
                    logger.info("=" * 85)
                    page.wait_for_timeout(1500)
                    return []

                # Collect product URLs directly from Samsung live search results first
                try:
                    page.wait_for_selector(
                        "a[href*='/in/smartphones/'], a[href*='/in/computers/'], a[href*='/in/tablets/'], a[href*='/in/tvs/'], .result-list a",
                        timeout=5000,
                    )
                except Exception:
                    pass
                for _ in range(2):
                    page.evaluate("window.scrollBy({top: 600, behavior: 'smooth'});")
                    page.wait_for_timeout(1000)
                search_page_urls = extract_search_result_urls(page.content(), page.url, "samsung", limit=candidate_cap)
                for su in search_page_urls:
                    clean_su = su.split("?")[0].rstrip("/") + "/"
                    if clean_su not in discovered_urls:
                        discovered_urls.append(clean_su)
                logger.info(f"↳ Extracted {len(discovered_urls)} product URL(s) from Samsung search results page.")

                terms = [t for t in re.findall(r"[a-z0-9]+", query_str.lower()) if len(t) > 1]
                generic_terms = {"all", "samsung", "products", "smartphones", "phones", "laptops", "tvs", "tablets", "refrigerators"}
                is_specific_query = query_str.lower() not in generic_terms and bool(terms)
                has_exact_url_match = is_specific_query and any(all(t in u.lower() for t in terms) for u in discovered_urls)

                cat_resolved = active_filter.category or resolve_category_from_query(query_str)
                if cat_resolved and (len(discovered_urls) < candidate_cap or (is_specific_query and not has_exact_url_match)):
                    catalog_url = CATEGORY_CATALOG_URLS.get(cat_resolved)
                    if catalog_url:
                        logger.info(f"💻 [3/5] Navigating to Samsung {cat_resolved.upper()} catalog ({catalog_url})...")
                        page.goto(catalog_url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2000)

                        logger.info(f"🔄 Checking for pagination ('View more' buttons) to load more {cat_resolved}...")
                        max_pagination_clicks = 5
                        clicks = 0
                        while clicks < max_pagination_clicks:
                            page.evaluate("window.scrollBy({top: 800, behavior: 'smooth'});")
                            page.wait_for_timeout(800)
                            clicked_any = False
                            view_more_buttons = page.query_selector_all(
                                "button:has-text('View more'), a:has-text('View more'), button:has-text('Show more'), .cta--show-more"
                            )
                            for btn in view_more_buttons:
                                if btn.is_visible():
                                    try:
                                        btn.click(timeout=2000)
                                        clicks += 1
                                        clicked_any = True
                                        logger.info(f"  ↳ Clicked 'View more' (Pagination expansion #{clicks})")
                                        page.wait_for_timeout(1200)
                                    except Exception:
                                        pass
                            if not clicked_any:
                                break

                        page_product_links = page.evaluate("""() => {
                            const anchors = Array.from(document.querySelectorAll('a[href]'));
                            return anchors.map(a => a.href);
                        }""")
                        for h in page_product_links:
                            clean_h = h.split("?")[0].rstrip("/") + "/"
                            if clean_h.startswith("https://www.samsung.com/in/") and any(
                                c in clean_h for c in ["/smartphones/", "/computers/", "/tablets/", "/tvs/", "/watches/", "/refrigerators/", "/monitors/", "/audio-devices/"]
                            ):
                                if not any(skip in clean_h for skip in ["/buy/", "/compare/", "buying-guide", "/accessories/", "all-"]):
                                    if clean_h not in discovered_urls:
                                        discovered_urls.append(clean_h)

                if len(discovered_urls) < candidate_cap or (is_specific_query and not any(all(t in u.lower() for t in terms) for u in discovered_urls)):
                    sitemap_urls = get_all_product_urls(category=cat_resolved)
                    for surl in sitemap_urls:
                        clean_s = surl.split("?")[0].rstrip("/") + "/"
                        if clean_s not in discovered_urls:
                            discovered_urls.append(clean_s)

                if is_specific_query and len(discovered_urls) > 1:
                    all_match = [u for u in discovered_urls if all(t in u.lower() for t in terms)]
                    any_match = [u for u in discovered_urls if any(t in u.lower() for t in terms) and u not in all_match]
                    if all_match:
                        discovered_urls = all_match + any_match
                    elif any_match:
                        discovered_urls = any_match

            if limit and limit > 0:
                needs_headroom = (
                    not active_filter.all_time
                    or bool(active_filter.min_price)
                    or bool(active_filter.max_price)
                    or bool(active_filter.min_rating)
                    or bool(active_filter.search)
                )
                candidate_limit = max(limit * 5, 10) if needs_headroom else limit
                discovered_urls = discovered_urls[:candidate_limit]

            logger.info(f"📋 Total {len(discovered_urls)} product URL(s) queued for inspection.")
            if not discovered_urls:
                logger.warning(
                    f"⚠️ No product URLs discovered for '{query_str}' on '{plat}'. "
                    f"Try a broader --search term, pass --url directly, or inspect site access."
                )

            # ------------------------------------------------------------------
            # Navigate Through Each Product ONE BY ONE
            # ------------------------------------------------------------------
            logger.info("🔎 [4/5] Navigating through each product ONE BY ONE...")
            db = (MongoManager(uri=mongo_uri) if mongo_uri else MongoManager()) if save_to_mongo else None
            mongo_connected = bool(db and db.connect())
            mongo_synced_count = 0
            if save_to_mongo and not mongo_connected:
                logger.warning("⚠️ MongoDB is offline or unreachable; continuing with JSON export.")

            skipped_reasons: List[str] = []
            for idx, product_url in enumerate(discovered_urls, 1):
                if limit and limit > 0 and not active_filter.sort_by and len(results_list) >= limit:
                    break
                try:
                    logger.info("-" * 80)
                    logger.info(f"▶️ [{idx}/{len(discovered_urls)}] Opening product: {product_url}")
                    page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500)

                    page.evaluate("window.scrollBy({top: 400, behavior: 'smooth'});")
                    page.wait_for_timeout(800)
                    expand_pdp_sections(page)
                    page.evaluate("window.scrollBy({top: 600, behavior: 'smooth'});")
                    page.wait_for_timeout(800)

                    try:
                        review_btn = page.query_selector("a[data-engname='review'], text='Review', [href*='#review']")
                        if review_btn and review_btn.is_visible():
                            review_btn.click()
                            page.wait_for_timeout(1000)
                        else:
                            page.evaluate("window.scrollBy({top: 1400, behavior: 'smooth'});")
                            page.wait_for_timeout(800)
                    except Exception:
                        page.evaluate("window.scrollBy({top: 1400, behavior: 'smooth'});")

                    html = page.content()
                    item_dict, rev_dict, err = scrape_product_and_reviews(
                        url=product_url,
                        platform=plat,
                        html_override=html,
                        days_cutoff=effective_days,
                    )
                    if err or not item_dict:
                        logger.warning(f"⚠️ Skipped {product_url}: {err}")
                        continue

                    flat = extract_flat_summary(item_dict, rev_dict)
                    ok, skip_reason = matches_filter(item_dict, active_filter, review_doc=rev_dict)
                    if not ok:
                        logger.info(f"⏳ Skipping [{flat['product_id']}]: {skip_reason}")
                        skipped_reasons.append(skip_reason)
                        continue

                    results_list.append(item_dict)
                    if rev_dict:
                        reviews_by_key[make_review_key(item_dict)] = rev_dict

                    if db and mongo_connected:
                        try:
                            db.bulk_upsert_products([item_dict], review_list=[rev_dict] if rev_dict else None)
                            mongo_synced_count += 1
                        except Exception as e_db:
                            logger.warning(f"⚠️ MongoDB upsert failed for [{flat['product_id']}]: {e_db}")

                    price_fmt = format_price_str(flat["price_details"])
                    logger.info(
                        f"✓ [{flat['platform']}/{flat['brand']}] [{flat['product_id']}] "
                        f"'{flat['title'][:34]}...' | Cat: {flat['category']} | Intro: {flat['introduced_date']} | "
                        f"Price: {price_fmt} | Reviews: {flat['total_reviews']}"
                    )

                except Exception as e_page:
                    logger.error(f"Error navigating {product_url}: {e_page}")

                if idx < len(discovered_urls):
                    page.wait_for_timeout(800)

            if db:
                db.close()

            if discovered_urls and not results_list and skipped_reasons:
                reason_blob = " ".join(skipped_reasons).lower()
                hint = " Tip: pass --include-undated or --all-time." if any(k in reason_blob for k in ("date", "undated", "days")) else ""
                logger.warning(
                    f"ℹ️ Visited {len(discovered_urls)} product(s), but 0 matched your filters "
                    f"(e.g. {skipped_reasons[0]}).{hint}"
                )

            results_list = filter_and_sort_products(results_list, active_filter, reviews_by_key=reviews_by_key)

            if output_file and results_list:
                matched_reviews = [
                    reviews_by_key[make_review_key(p)]
                    for p in results_list
                    if make_review_key(p) in reviews_by_key
                ]
                save_laptops_to_json(results_list, output_file, review_list=matched_reviews)

            logger.info("=" * 85)
            logger.info(
                f"🎉 [5/5] COMPLETE! Extracted {len(results_list)} filtered product(s). "
                f"MongoDB synced: {mongo_synced_count} | JSON: '{output_file}'"
            )
            logger.info("=" * 85)

        finally:
            browser.close()
            logger.info("Browser closed cleanly.")

    return results_list


if __name__ == "__main__":
    run_automated_navigation(headless=False)
