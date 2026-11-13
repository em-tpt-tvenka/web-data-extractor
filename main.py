"""
Main Orchestration Script for Multi-Retail & Samsung E-Commerce Scraper.
Supports Samsung India plus third-party retail websites (Croma, Reliance Digital,
Vijay Sales, Flipkart, Amazon India, Alibaba, Walmart, JioMart, Best Buy, or any Schema.org/OpenGraph URL).
Provides two-step interactive platform -> product search, fast HTTP batch scraping,
Playwright rendering, visual browser navigation, unified search & filter parameters,
and MongoDB / JSON / CSV persistence.
"""

import os
import sys
import time
import argparse
import logging
from typing import List, Dict, Any, Optional, Tuple

import requests

from config import (
    DELAY_BETWEEN_REQUESTS,
    OUTPUT_JSON_PATH,
    PLATFORM_CONFIGS,
    resolve_platform_name,
    search_platforms,
)
from database import MongoManager, save_laptops_to_json, load_reviews_map_for_products_json
from models import extract_flat_summary, get_reviews_json_path, make_review_key
from scraper.filters import (
    ProductFilter,
    add_filter_args,
    filter_and_sort_products,
    filter_from_args,
    format_price_str,
    matches_filter,
)
from scraper.platforms import discover_urls_for_platform, scrape_product_and_reviews
from scraper.playwright_scraper import PlaywrightSamsungScraper
from scraper.browser_navigator import run_automated_navigation

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("samsung_scraper")


def scrape_product_and_reviews_http(
    url: str,
    session: requests.Session,
    days_cutoff: Optional[int] = 365,
    platform: str = "auto",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Scrape a single product page into Schema v2.0.0 Product Details + Review Details via HTTP."""
    prod_v2, rev_v2, err = scrape_product_and_reviews(
        url=url,
        platform=platform,
        session=session,
        days_cutoff=days_cutoff,
    )
    if err or not prod_v2:
        raise RuntimeError(err or f"Failed to extract product data from {url}")

    flat = extract_flat_summary(prod_v2, rev_v2)
    media_info = f"{flat['images_count']} images, {flat['videos_count']} videos"
    p_display = format_price_str(flat["price_details"])
    logger.info(
        f"✓ [HTTP/{flat['platform']}/{flat['brand']}] [{flat['product_id']}] "
        f"'{flat['title'][:34]}...' | Cat: {flat['category']} | Intro: {flat['introduced_date']} | "
        f"Price: {p_display} | Media: {media_info} | Reviews: {flat['total_reviews']}"
    )
    return prod_v2, rev_v2


def scrape_laptop_http(
    url: str,
    session: requests.Session,
    days_cutoff: Optional[int] = 365,
    platform: str = "auto",
) -> Dict[str, Any]:
    """Backward-compatible helper returning only the Schema v2.0.0 Product Details doc."""
    prod_v2, _ = scrape_product_and_reviews_http(
        url=url,
        session=session,
        days_cutoff=days_cutoff,
        platform=platform,
    )
    return prod_v2


scrape_product_http = scrape_laptop_http


def _print_platforms(query: Optional[str] = None) -> List[Dict[str, Any]]:
    """Print available or searched retail platforms in a structured table."""
    matches = search_platforms(query, include_generic=True)
    title = f"RETAIL PLATFORMS MATCHING '{query}'" if query else "SUPPORTED RETAIL PLATFORMS"
    print("\n" + "=" * 95)
    print(f" 🌐 {title} ({len(matches)} found)")
    print("=" * 95)
    if not matches:
        print(f"No configured platform matched '{query}'. Use 'generic' to scrape any direct URL.")
        print("=" * 95 + "\n")
        return []

    print(f"{'#':<3} {'KEY':<16} {'PLATFORM NAME':<22} {'DOMAIN':<20} {'CUR':<5} {'SEARCH QUERY EXAMPLE'}")
    print("-" * 95)
    for idx, cfg in enumerate(matches, 1):
        key = cfg["key"]
        name = cfg.get("name", key)[:21]
        domain = (cfg.get("domain") or "any URL")[:19]
        curr = cfg.get("currency", "INR")
        aliases = ", ".join((cfg.get("aliases") or [key])[:3])
        print(f"{idx:<3} {key:<16} {name:<22} {domain:<20} {curr:<5} {aliases}")
    print("=" * 95 + "\n")
    return matches


def _print_cli_results(
    results: List[Dict[str, Any]],
    title: str,
    output_file: str,
    reviews_by_key: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> None:
    """Print concise CLI summary and both Schema v2.0.0 product & review file paths."""
    print("\n" + "=" * 85)
    print(f"📦 {title}")
    print(f"Total Matching Products: {len(results)}")
    if not results:
        print("Result: [] (Empty List)")
        print("ℹ️ Tip: No new items matched the active filters, so the output JSON file was not overwritten.")
        print("   Try 'all' (or --all-time --include-undated) if products were excluded by the 365-day / date filter.")
    else:
        rev_map = reviews_by_key if reviews_by_key is not None else load_reviews_map_for_products_json(output_file)
        for idx, r in enumerate(results, 1):
            key = make_review_key(r)
            rev_doc = rev_map.get(key) or rev_map.get(key[1])
            flat = extract_flat_summary(r, rev_doc)
            p_info = format_price_str(flat["price_details"])
            rev_info = flat["total_reviews"]
            avg_rat = flat["average_rating"]
            rat_str = f"{rev_info} ({avg_rat}★)" if avg_rat is not None else f"{rev_info}"
            print(
                f"  {idx}. [{flat['platform']}/{flat['brand']}] [{flat['product_id']}] ({flat['category'] or 'electronics'}) "
                f"{flat['title']} | Date: {flat['introduced_date'] or 'N/A'} | Price: {p_info} | Reviews: {rat_str}"
            )
        reviews_json_path = get_reviews_json_path(output_file)
        print(f"\n💾 Products JSON:     '{os.path.abspath(output_file)}'")
        print(f"💾 Reviews JSON:      '{os.path.abspath(reviews_json_path)}'")
    print("=" * 85 + "\n")


def run_pipeline(
    limit: Optional[int] = None,
    single_url: Optional[str] = None,
    urls_list: Optional[List[str]] = None,
    platform: str = "samsung",
    search_query: Optional[str] = None,
    category: Optional[str] = None,
    days_cutoff: Optional[int] = 365,
    past_year_only: bool = True,
    use_playwright: bool = False,
    save_to_mongo: bool = True,
    mongo_uri: Optional[str] = None,
    output_file: str = OUTPUT_JSON_PATH,
    product_filter: Optional[ProductFilter] = None,
) -> List[Dict[str, Any]]:
    """
    Executes the end-to-end multi-retail scraping, filtering, JSON/CSV export, and MongoDB sync workflow.
    """
    req_platform = resolve_platform_name((platform or "samsung").strip().lower())
    active_filter = product_filter or ProductFilter(
        platform=None if req_platform in ("auto", "samsung", "all", "generic") else req_platform,
        search=search_query,
        category=category,
        days=days_cutoff if days_cutoff is not None else 365,
        all_time=not past_year_only,
        limit=limit,
    )
    effective_days = None if active_filter.all_time else active_filter.days
    eff_limit = active_filter.limit if active_filter.limit is not None else limit

    scraped_products: List[Dict[str, Any]] = []
    reviews_by_key: Dict[Any, Dict[str, Any]] = {}
    target_platform = req_platform if req_platform not in ("all", "") else "samsung"

    # Step 1: Discover Target URLs
    if single_url:
        target_urls = [single_url]
        discovery_warning = None
    elif urls_list:
        target_urls = list(urls_list)
        discovery_warning = None
    else:
        discovery_cap = max(eff_limit * 8, 40) if (eff_limit and eff_limit > 0) else 40
        logger.info(
            f"Discovering product URLs on platform='{target_platform}' "
            f"(category='{active_filter.category or 'all'}', search='{active_filter.search or ''}')..."
        )
        target_urls, discovery_warning = discover_urls_for_platform(
            platform=target_platform,
            category=active_filter.category,
            search_query=active_filter.search,
            limit=discovery_cap,
            use_playwright=use_playwright,
        )
        if discovery_warning:
            logger.warning(f"⚠️ {discovery_warning}")

    if not target_urls:
        logger.warning("No target product URLs to scrape.")
        return []

    # Cap total inspected candidate URLs politely while leaving buffer for filters
    if eff_limit and eff_limit > 0:
        max_candidates = eff_limit if (active_filter.all_time and active_filter.include_undated and not active_filter.sort_by and active_filter.min_price is None and active_filter.max_price is None and active_filter.min_rating is None) else max(eff_limit * 8, 30)
        target_urls = target_urls[:max_candidates]

    logger.info(
        f"Inspecting up to {len(target_urls)} URL(s) on platform='{target_platform}' "
        f"(Engine: {'Playwright' if use_playwright else 'Fast HTTP'} | "
        f"Date Filter: {'All Time' if active_filter.all_time else f'Last {active_filter.days} days'})"
    )

    # Step 2: Scrape & Filter Each Product + Reviews Pair
    if use_playwright:
        pw_scraper = PlaywrightSamsungScraper(headless=True)
        pw_scraper.start()
        try:
            for idx, url in enumerate(target_urls, 1):
                if eff_limit and eff_limit > 0 and not active_filter.sort_by and len(scraped_products) >= eff_limit:
                    break
                try:
                    logger.info(f"[{idx}/{len(target_urls)}] Playwright rendering: {url}")
                    item_dict, review_dict = pw_scraper.scrape_product_and_reviews(
                        url,
                        days_cutoff=effective_days,
                        platform=target_platform,
                    )
                    flat = extract_flat_summary(item_dict, review_dict)
                    ok, skip_reason = matches_filter(item_dict, active_filter, review_doc=review_dict)
                    if not ok:
                        logger.info(f"Skipping [{flat['product_id']}]: {skip_reason}")
                        continue
                    scraped_products.append(item_dict)
                    if review_dict:
                        reviews_by_key[make_review_key(item_dict)] = review_dict
                except Exception as e:
                    logger.error(f"Failed to scrape {url} via Playwright: {e}")
                if idx < len(target_urls):
                    time.sleep(DELAY_BETWEEN_REQUESTS)
        finally:
            pw_scraper.close()
    else:
        session = requests.Session()
        try:
            for idx, url in enumerate(target_urls, 1):
                if eff_limit and eff_limit > 0 and not active_filter.sort_by and len(scraped_products) >= eff_limit:
                    break
                try:
                    logger.info(f"[{idx}/{len(target_urls)}] Processing: {url}")
                    item_dict, review_dict = scrape_product_and_reviews_http(
                        url,
                        session,
                        days_cutoff=effective_days,
                        platform=target_platform,
                    )
                    flat = extract_flat_summary(item_dict, review_dict)
                    ok, skip_reason = matches_filter(item_dict, active_filter, review_doc=review_dict)
                    if not ok:
                        logger.info(f"Skipping [{flat['product_id']}]: {skip_reason}")
                        continue
                    scraped_products.append(item_dict)
                    if review_dict:
                        reviews_by_key[make_review_key(item_dict)] = review_dict
                except Exception as e:
                    logger.error(f"Failed to scrape {url}: {e}")
                if idx < len(target_urls):
                    time.sleep(DELAY_BETWEEN_REQUESTS)
        finally:
            session.close()

    # Step 3: Sort & Apply Final Limit
    scraped_products = filter_and_sort_products(scraped_products, active_filter, reviews_by_key=reviews_by_key)
    matched_reviews = [
        reviews_by_key[make_review_key(p)]
        for p in scraped_products
        if make_review_key(p) in reviews_by_key
    ]

    # Step 4: Save Separate Product Details + Review Details JSON & CSVs
    if scraped_products:
        save_laptops_to_json(scraped_products, filepath=output_file, review_list=matched_reviews)
        logger.info(
            f"Saved {len(scraped_products)} product(s) & {len(matched_reviews)} review bundle(s) [Schema v2.0.0] to: '{output_file}'"
        )
    else:
        logger.info("No products matched the active filters; JSON file left unchanged.")

    # Step 5: MongoDB Ingestion
    if save_to_mongo and scraped_products:
        logger.info("Syncing documents to MongoDB...")
        mongo_kwargs = {"uri": mongo_uri} if mongo_uri else {}
        mongo = MongoManager(**mongo_kwargs)
        if mongo.connect():
            try:
                stats = mongo.bulk_upsert_products(scraped_products, review_list=matched_reviews)
                logger.info(
                    f"MongoDB sync complete! "
                    f"New: {stats['inserted']}, Updated: {stats['modified']}, "
                    f"Matched: {stats['matched']}, Total: {stats['total']}, Reviews: {stats['reviews_upserted']}"
                )
            except Exception as e:
                logger.error(f"Error during MongoDB batch write: {e}")
            finally:
                mongo.close()
        else:
            logger.warning("MongoDB is offline or unreachable. Data is saved in the JSON output.")

    logger.info(f"Pipeline finished! Total matching items processed: {len(scraped_products)}")
    return scraped_products


def _prompt_optional_float(prompt_text: str) -> Optional[float]:
    raw = input(prompt_text).strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        print(f"  ⚠️ Ignoring invalid number '{raw}'.")
        return None


def interactive_platform_and_product_search(args: argparse.Namespace) -> None:
    """
    Two-step interactive workflow:
      Step 1: Search / pick the retail platform (Samsung, Walmart, Alibaba, JioMart, Best Buy, Croma, Flipkart, Amazon, etc.)
      Step 2: Search products on that platform & apply search filters, then scrape data.
    """
    all_platforms = _print_platforms(None)

    selected_platform: Optional[str] = None
    candidates = all_platforms
    while not selected_platform:
        try:
            raw_plat = input("Step 1 — Search retail platform by name, keyword, or # (Enter = samsung): ").strip()
        except EOFError:
            selected_platform = "samsung"
            break

        if not raw_plat:
            selected_platform = "samsung"
            break

        if raw_plat.isdigit():
            idx = int(raw_plat)
            if 1 <= idx <= len(candidates):
                selected_platform = candidates[idx - 1]["key"]
                break
            print(f"  ⚠️ Pick a number between 1 and {len(candidates)}.")
            continue

        resolved = resolve_platform_name(raw_plat)
        if resolved in PLATFORM_CONFIGS:
            # Check if ambiguous short query matched more than one platform when not an exact key/alias
            matches = search_platforms(raw_plat, include_generic=True)
            exact_hit = raw_plat.lower() in PLATFORM_CONFIGS or any(
                raw_plat.lower() in [a.lower() for a in cfg.get("aliases", [])]
                for cfg in PLATFORM_CONFIGS.values()
            )
            if exact_hit or len(matches) == 1:
                selected_platform = resolved
                break
            if len(matches) > 1:
                candidates = _print_platforms(raw_plat)
                print("  Multiple platforms matched. Enter the # number or exact key above.")
                continue

        print(f"  ⚠️ No platform matched '{raw_plat}'. Try typing e.g. 'walmart', 'jio', 'best buy', 'alibaba', or 'generic'.")

    plat_cfg = PLATFORM_CONFIGS[selected_platform]
    print(f"\n✓ Selected Platform: {plat_cfg['name']} ({selected_platform}) | Currency: {plat_cfg.get('currency', 'INR')}\n")

    single_url: Optional[str] = None
    search_query: Optional[str] = None
    try:
        while True:
            prompt_msg = (
                f"Step 2 — Enter product URL to scrape ({plat_cfg['name']}): "
                if selected_platform == "generic"
                else f"Step 2 — Search products inside {plat_cfg['name']} (or paste a full URL): "
            )
            raw_query = input(prompt_msg).strip()
            if not raw_query:
                if selected_platform == "samsung":
                    search_query = "laptops"
                    break
                print("  ⚠️ Please enter a product search term or full URL.")
                continue
            if raw_query.startswith(("http://", "https://")):
                single_url = raw_query
            else:
                search_query = raw_query
            break

        # Step 3: Limit, optional filters, and browser selection (no required filters by default)
        raw_limit = input("Step 3 — Max products to scrape [default: 5]: ").strip()
        limit_val = int(raw_limit) if raw_limit.isdigit() else 5

        category_val = None
        all_time = True
        days_val = 365
        include_undated = True
        min_price = None
        max_price = None
        min_rating = None
        sort_by = None

        want_filters = input("  Apply extra filters (date/price/rating)? [y/N]: ").strip().lower() == "y"
        if want_filters:
            category_val = input("  Category filter (optional, e.g. smartphones, laptops | Enter = any): ").strip() or None
            raw_days = input("  Date filter — last N days, or 'all' for all-time [default: all]: ").strip().lower()
            all_time = raw_days in ("", "all", "all-time", "0", "none")
            days_val = 365 if (all_time or not raw_days.isdigit()) else int(raw_days)

            raw_undated = input("  Keep undated listings if store hides publish date? [Y/n]: ").strip().lower()
            include_undated = raw_undated != "n"

            min_price = _prompt_optional_float("  Minimum price (Enter = any): ")
            max_price = _prompt_optional_float("  Maximum price (Enter = any): ")
            min_rating = _prompt_optional_float("  Minimum rating 0-5 (Enter = any): ")
            sort_by = input("  Sort order (price_asc/price_desc/rating_desc/reviews_desc/date_desc | Enter = none): ").strip().lower() or None
            if sort_by not in (None, "price_asc", "price_desc", "rating_desc", "reviews_desc", "date_desc", "name_asc"):
                print(f"  ⚠️ Ignoring unsupported sort option '{sort_by}'.")
                sort_by = None

        default_mode = "5" if (args.batch or args.headless) else "1"
        raw_mode = input(
            f"  Browser to open — (1) Auto [Firefox/Edge/Chrome], (2) Firefox, (3) Edge, (4) Chrome, (5) Headless [{default_mode}]: "
        ).strip() or default_mode
        browser_map = {"1": args.browser, "2": "firefox", "3": "edge", "4": "chrome"}
        browser_choice = browser_map.get(raw_mode, args.browser)
        visual_mode = raw_mode != "5"
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    flt = ProductFilter(
        platform=None if selected_platform in ("auto", "samsung", "generic") else selected_platform,
        search=search_query,
        category=category_val,
        days=days_val,
        all_time=all_time,
        include_undated=include_undated,
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
        sort_by=sort_by,
        limit=limit_val,
    )

    if visual_mode:
        try:
            results = run_automated_navigation(
                headless=args.headless,
                search_query=search_query or category_val or "laptops",
                target_laptop_url=single_url,
                limit=limit_val,
                past_year_only=not all_time,
                save_to_mongo=not args.no_mongo,
                mongo_uri=args.mongo_uri,
                output_file=args.output,
                platform=selected_platform,
                product_filter=flt,
                browser_name=browser_choice,
            )
            _print_cli_results(results, f"FINAL OUTPUT ({selected_platform} | '{search_query or single_url}')", args.output)
        except Exception as e:
            logger.warning(f"Visual browser navigation failed ({e}). Falling back to pipeline...")
            use_pw = args.playwright or (selected_platform != "samsung")
            results = run_pipeline(
                limit=limit_val,
                single_url=single_url,
                platform=selected_platform,
                search_query=search_query,
                category=category_val,
                days_cutoff=days_val,
                past_year_only=not all_time,
                use_playwright=use_pw,
                save_to_mongo=not args.no_mongo,
                mongo_uri=args.mongo_uri,
                output_file=args.output,
                product_filter=flt,
            )
            _print_cli_results(results, f"FALLBACK OUTPUT ({selected_platform} | '{search_query or single_url}')", args.output)
    else:
        use_pw = args.playwright or (selected_platform != "samsung")
        results = run_pipeline(
            limit=limit_val,
            single_url=single_url,
            platform=selected_platform,
            search_query=search_query,
            category=category_val,
            days_cutoff=days_val,
            past_year_only=not all_time,
            use_playwright=use_pw,
            save_to_mongo=not args.no_mongo,
            mongo_uri=args.mongo_uri,
            output_file=args.output,
            product_filter=flt,
        )
        _print_cli_results(results, f"SEARCH OUTPUT ({selected_platform} | '{search_query or single_url}')", args.output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Multi-Retail & Samsung E-Commerce Product Scraper, Platform Search & Filter Pipeline"
    )
    parser.add_argument("--list-platforms", action="store_true", help="List all configured retail platforms and aliases")
    parser.add_argument("--find-platform", type=str, default=None, help="Search retail platforms by keyword (e.g. 'wall mart', 'jio', 'best buy', 'alibaba')")
    parser.add_argument("-i", "--interactive", action="store_true", help="Launch two-step interactive search: (1) search platform -> (2) search & filter products")
    parser.add_argument("--batch", action="store_true", help="Run automated batch scraping pipeline")
    parser.add_argument("--visual", action="store_true", help="Launch visual interactive Firefox browser navigator")
    parser.add_argument("--playwright", action="store_true", help="Use Playwright browser engine in batch mode (search & PDP)")
    parser.add_argument("--limit", type=int, default=None, help="Maximum matching products to extract")
    parser.add_argument("--url", type=str, default=None, help="Scrape a specific product URL (any retail site)")
    parser.add_argument("--urls", nargs="+", default=None, help="Scrape multiple product URLs across any retail site")
    parser.add_argument("--no-mongo", action="store_true", help="Skip MongoDB ingestion (JSON output only)")
    parser.add_argument("--mongo-uri", type=str, default=None, help="Override MongoDB connection URI")
    parser.add_argument("--output", type=str, default=OUTPUT_JSON_PATH, help="Path to output JSON file")
    parser.add_argument("--headless", action="store_true", help="Run visual browser navigation in headless mode")
    parser.add_argument(
        "--browser",
        type=str,
        default="auto",
        choices=["auto", "firefox", "edge", "chrome", "chromium"],
        help="Preferred browser engine for visual navigation (auto tries Firefox -> Edge -> Chrome -> Chromium)",
    )
    add_filter_args(parser, include_search=True)

    args = parser.parse_args()

    if args.list_platforms:
        _print_platforms(None)
        sys.exit(0)

    if args.find_platform:
        _print_platforms(args.find_platform)
        sys.exit(0)

    if args.interactive or (len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty()):
        interactive_platform_and_product_search(args)
        sys.exit(0)

    flt = filter_from_args(args, for_query=False)
    target_platform = resolve_platform_name(args.platform) if args.platform else (flt.platform or "samsung")

    run_visual = args.visual or args.headless or (len(sys.argv) == 1) or (
        not args.batch
        and not args.url
        and not args.urls
        and not args.playwright
        and (args.search is not None or args.platform is not None or args.category is not None)
    )

    if run_visual:
        search_term = args.search or args.category or "laptops"
        logger.info("▶️ Starting Automated Visual Browser Navigation...")
        logger.info(f"🔎 Target Platform: '{target_platform}' | Query: '{search_term}'")
        try:
            results = run_automated_navigation(
                headless=args.headless,
                search_query=search_term,
                target_laptop_url=args.url,
                limit=args.limit,
                past_year_only=not args.all_time,
                save_to_mongo=not args.no_mongo,
                mongo_uri=args.mongo_uri,
                output_file=args.output,
                platform=target_platform,
                product_filter=flt,
                browser_name=args.browser,
            )
            _print_cli_results(results, f"FINAL OUTPUT ({target_platform} | '{search_term}')", args.output)
        except Exception as e:
            logger.warning(f"Browser navigation failed ({e}). Falling back to HTTP pipeline...")
            results = run_pipeline(
                limit=args.limit or 3,
                single_url=args.url,
                platform=target_platform,
                search_query=args.search or (None if args.url else search_term),
                category=args.category,
                days_cutoff=args.days,
                past_year_only=not args.all_time,
                save_to_mongo=not args.no_mongo,
                mongo_uri=args.mongo_uri,
                output_file=args.output,
                product_filter=flt,
            )
            _print_cli_results(results, f"FALLBACK HTTP OUTPUT ({target_platform})", args.output)
    else:
        results = run_pipeline(
            limit=args.limit,
            single_url=args.url,
            urls_list=args.urls,
            platform=target_platform,
            search_query=args.search,
            category=args.category,
            days_cutoff=args.days,
            past_year_only=not args.all_time,
            use_playwright=args.playwright,
            save_to_mongo=not args.no_mongo,
            mongo_uri=args.mongo_uri,
            output_file=args.output,
            product_filter=flt,
        )
        _print_cli_results(results, f"BATCH PIPELINE OUTPUT ({target_platform})", args.output)
