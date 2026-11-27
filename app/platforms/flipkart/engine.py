from __future__ import annotations

import asyncio
import itertools
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from app.core.logging import get_logger
from app.platforms import common
from app.platforms.browser import proxy_settings, random_user_agent
from app.platforms.browser_launch import with_chrome_fallback
from app.platforms.flipkart import parsing, scripts

_log = get_logger("flipkart.engine")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SEARCH_SORT = {
    "relevance": "relevance",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
    "rating": "popularity",
    "newest": "recency_desc",
}
_REVIEW_SORT_PARAM = {
    "helpful": None,
    "recent": "MOST_RECENT",
    "positive": "POSITIVE_FIRST",
    "negative": "NEGATIVE_FIRST",
}
_REVIEW_SORT_CHIP = {"recent": "Latest", "positive": "Positive", "negative": "Negative"}

_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-IN','en'] });
window.chrome = { runtime: {} };
"""

_BLOCK_RE = re.compile(r"captcha|access denied|are you a human|unusual traffic", re.I)

# Bounds for the sibling-link variant crawl (see _crawl_variant_details):
# generous enough for a real 4-colour x 2-storage (8 SKU) product with room
# to spare, while guaranteeing a product with an unusually large combination
# matrix can't turn one product into an unbounded crawl.
_VARIANT_MAX_DEPTH = 3
_VARIANT_MAX_TOTAL = 24


@dataclass
class EngineConfig:
    search_text: str
    marketplace: str = "flipkart.com"
    sort_by: str = "relevance"
    min_price: float | None = None
    max_price: float | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    max_pages: int = 1
    max_products: int = 20
    include_sponsored: bool = False
    fetch_reviews: bool = True
    review_sort: str = "recent"
    verified_purchase_only: bool = False
    # Start of the requested date window (None = collect all time). Lets the
    # "recent" (MOST_RECENT) sort stop paging early once a page is entirely
    # older than this; the other sort orders are not date-ordered and are
    # never early-stopped by it.
    review_window_start: date | None = None
    review_retries: int = 3           # retries of a page Flipkart redirected to /login
    review_retry_backoff: float = 4.0  # seconds, doubled per retry
    # Flipkart has no existing per-run product concurrency knob to reuse here:
    # products are already scraped strictly sequentially, one browser tab at a
    # time (see run()). This only bounds how many of ONE product's OTHER real
    # variants (independent per-variant scraping) are visited in parallel, in
    # new tabs of the SAME browser context.
    variant_detail_concurrency: int = 2
    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 60_000
    debug_dir: str | None = None


class FlipkartEngine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.pages_processed = 0
        self.reviews_incomplete = 0
        self.review_listings_failed = 0

    async def run(self) -> list[dict]:
        cfg = self.cfg
        models: list[dict] = []
        _log.info(
            "event=engine_start search=%r sort=%s max_pages=%s max_products=%s "
            "fetch_reviews=%s headless=%s channel=%s",
            cfg.search_text, cfg.sort_by, cfg.max_pages, cfg.max_products,
            cfg.fetch_reviews, cfg.headless, cfg.browser_channel,
        )
        launch_args = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
        viewport = {"width": 1440, "height": 900}

        async with async_playwright() as pw:
            browser = None
            mode = "persistent_profile" if cfg.chrome_profile_dir else "fresh_context"
            _log.info("event=browser_launching mode=%s channel=%s headless=%s",
                      mode, cfg.browser_channel, cfg.headless)

            async def _do_launch():
                if cfg.chrome_profile_dir:
                    return await pw.chromium.launch_persistent_context(
                        cfg.chrome_profile_dir, channel=cfg.browser_channel,
                        headless=cfg.headless, args=launch_args, user_agent=random_user_agent(UA),
                        locale="en-IN", viewport=viewport, proxy=proxy_settings())
                browser_ = await pw.chromium.launch(
                    headless=cfg.headless, channel=cfg.browser_channel, args=launch_args,
                    proxy=proxy_settings())
                return await browser_.new_context(user_agent=random_user_agent(UA), locale="en-IN", viewport=viewport)

            try:
                ctx = await with_chrome_fallback(
                    _do_launch, platform="flipkart", pw=pw, profile_dir=cfg.chrome_profile_dir,
                    headless=cfg.headless, user_agent=random_user_agent(UA), locale="en-IN",
                    proxy=proxy_settings(), viewport=viewport, extra_args=launch_args,
                )
            except Exception as exc:
                _log.error("event=browser_launch_failed error=%s", exc)
                raise
            await ctx.add_init_script(_STEALTH)
            _log.info("event=browser_launched mode=%s", mode)

            try:
                items = await self._search(ctx)
                for item in items:
                    if cfg.max_products > 0 and len(models) >= cfg.max_products:
                        break
                    page = await ctx.new_page()
                    try:
                        data = await self._scrape_product(page, item)
                        if data:
                            data["variant_details"] = await self._scrape_all_variant_details(ctx, data)
                            models.append(data)
                            _log.info("event=product_done id=%s reviews=%d variants=%d",
                                      data.get("product_id") or data.get("item_id"),
                                      len(data.get("reviews", [])), len(data["variant_details"]))
                    except Exception as exc:
                        _log.error("event=product_failed url=%s error=%s",
                                   (item.get("url") or "")[:90], exc)
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass
                    await asyncio.sleep(random.uniform(1.0, 2.0))
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                _log.info("event=browser_closed")

        _log.info("event=engine_done products=%d", len(models))
        return models

    def _search_url(self, page_no: int) -> str:
        base = self.cfg.marketplace.strip() or "flipkart.com"
        if not base.startswith("http"):
            base = "https://" + (base if base.startswith("www.") else "www." + base)
        sort = _SEARCH_SORT.get(self.cfg.sort_by, "relevance")
        params = f"q={quote_plus(self.cfg.search_text)}&sort={sort}&page={page_no}"
        if self.cfg.min_price is not None:
            params += f"&p%5B%5D=facets.price_range.from%3D{int(self.cfg.min_price)}"
        if self.cfg.max_price is not None:
            params += f"&p%5B%5D=facets.price_range.to%3D{int(self.cfg.max_price)}"
        return f"{base}/search?{params}"

    async def _load_search_page(self, page, url: str, attempts: int = 3) -> tuple[list, bool]:
        """Load one search-results page with retries. A one-off navigation
        error or bot-check challenge must not silently zero out the whole
        job: confirmed live that a single failed/blocked attempt on page 1
        (with no retry, and max_pages=1 so no other page to fall back to)
        left a real job with 0 products discovered, while the exact same
        search succeeded moments later on a fresh attempt - i.e. a transient
        failure, not a real "no results" or a code regression. Returns
        (results, blocked); blocked=True only once every attempt either
        errored or hit the block-page check."""
        for attempt in range(1, attempts + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                await page.wait_for_timeout(2000)
                await self._dismiss_login(page)
                await self._scroll_page(page)
                body = await self._inner_text(page)
                if _BLOCK_RE.search(body):
                    _log.warning("event=search_page_blocked attempt=%d/%d url=%s", attempt, attempts, url[:110])
                    await page.wait_for_timeout(2500 * attempt)
                    continue
                return await self._ev(page, scripts.JS_SEARCH_RESULTS, []) or [], False
            except Exception as exc:
                _log.warning("event=search_page_failed attempt=%d/%d url=%s error=%s",
                             attempt, attempts, url[:110], exc)
                await page.wait_for_timeout(2000 * attempt)
        return [], True

    async def _search(self, ctx) -> list[dict]:
        cfg = self.cfg
        page_limit = cfg.max_pages if cfg.max_pages > 0 else 10
        excludes = [k.lower() for k in cfg.exclude_keywords if k]
        found: dict[str, dict] = {}
        page = await ctx.new_page()
        try:
            for page_no in range(1, page_limit + 1):
                url = self._search_url(page_no)
                _log.info("event=search_page page=%d url=%s", page_no, url[:110])
                results, blocked = await self._load_search_page(page, url)
                if blocked:
                    _log.warning("event=search_blocked page=%d", page_no)
                    break
                self.pages_processed += 1
                added = 0
                for r in results:
                    pid = r.get("product_id") or r.get("item_id")
                    if not pid or pid in found:
                        continue
                    if not cfg.include_sponsored and r.get("is_sponsored"):
                        continue
                    title = (r.get("title") or "").lower()
                    if excludes and any(k in title for k in excludes):
                        continue
                    found[pid] = r
                    added += 1
                    if cfg.max_products > 0 and len(found) >= cfg.max_products:
                        break
                _log.info("event=search_extract page=%d found=%d total=%d", page_no, added, len(found))
                if cfg.max_products > 0 and len(found) >= cfg.max_products:
                    break
                if not results:
                    break
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return list(found.values())

    async def _scrape_product_page(self, page: Page, item: dict) -> dict | None:
        """Everything the product page itself shows - identity, price,
        ratings, offers, specs, media, swatch variants - WITHOUT reviews.
        Used both for the main product (``_scrape_product`` below appends
        reviews after this) and, unchanged, to independently visit another
        real variant's own page (``_scrape_variant_details``), which
        deliberately does not re-scrape reviews for every variant."""
        cfg = self.cfg
        url = item["url"]
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
        await page.wait_for_timeout(2500)
        await self._dismiss_login(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await self._scroll_page(page)
        await self._expand_sections(page)
        await page.wait_for_timeout(600)

        text = await self._inner_text(page)
        if _BLOCK_RE.search(text):
            _log.warning("event=product_blocked url=%s", url[:90])
            return None

        data: dict = {"url": page.url, **parsing.get_ids(page.url)}
        data["is_sponsored"] = bool(item.get("is_sponsored"))
        data["scraped_at"] = datetime.now(timezone.utc).isoformat()

        ld = await self._ev(page, scripts.JS_JSONLD, []) or []
        flat_ld: list = []
        for block in ld:
            if isinstance(block, dict):
                flat_ld.extend(block.get("@graph", [block]))
            elif isinstance(block, list):
                flat_ld.extend(block)
        prod = next((b for b in flat_ld if isinstance(b, dict) and b.get("@type") == "Product"), {})
        offer = prod.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        agg = prod.get("aggregateRating") or {}

        data["product_name"] = prod.get("name") or await self._first_text(page, "h1")
        brand = prod.get("brand")
        data["brand"] = brand.get("name") if isinstance(brand, dict) else brand
        data["sku"] = prod.get("sku")
        data["description"] = prod.get("description")

        # Strip the "Digital plans with this product" upsell block (Google One,
        # Air fryer coupons, etc.) before any price/offer regex runs on the
        # page text - confirmed live that its own "Extra ₹X Off on 1st
        # quarter" text otherwise gets misread as the PRODUCT's
        # extra_discount_amount, and it sits between the real offers section
        # and "Similar Products", so leaving it in also risks bleeding into
        # section-scoped extraction if an expected end-marker is ever absent.
        clean_text = text.split("Digital plans with this product")[0]

        data["price"] = self._build_price(await self._ev(page, scripts.JS_PRICES, {}) or {}, clean_text, offer)
        data["ratings"] = self._build_ratings(agg, text)
        data["offers"] = self._build_offers(clean_text)
        await self._fill_emi_offers(page, data["offers"])
        data["delivery"], data["seller"], data["warranty_and_policies"] = self._build_logistics(clean_text)

        # Real product-highlight bullets only. Do NOT fall back to the marketing
        # description here: that paragraph already lives in content.text, and
        # copying it into features duplicated it into highlights + keywords.
        features = await self._ev(page, scripts.JS_HIGHLIGHTS, []) or []
        data["features"] = features

        data.update(await self._build_specs(page, text))
        data["manufacturer"] = parsing.extract_manufacturer(text, data.get("specifications_flat", {}))

        imgs, vids = await self._collect_media(page)
        if prod.get("image"):
            extra = prod["image"] if isinstance(prod["image"], list) else [prod["image"]]
            imgs = parsing.dedupe_images(imgs + extra)
        data["images"] = imgs
        data["videos"] = vids
        data["variants"] = await self._ev(page, scripts.JS_VARIANTS, {}) or {}
        # Internal only (never surfaced to callers of to_records/the API) -
        # feeds _scrape_all_variant_details's sibling-link crawl; captured
        # here, on this exact page/navigation, so every visited SKU (parent
        # and every variant) supplies its own further siblings the same way.
        data["sibling_links"] = await self._ev(page, scripts.JS_SIBLING_LINKS, []) or []
        return data

    async def _scrape_product(self, page: Page, item: dict) -> dict | None:
        cfg = self.cfg
        data = await self._scrape_product_page(page, item)
        if data is None:
            return None

        data["review_summary"] = {}
        data["reviews"] = []
        if cfg.fetch_reviews:
            try:
                summary, reviews = await self._scrape_reviews(page, data["url"])
                data["review_summary"] = summary
                data["reviews"] = reviews
            except Exception as exc:
                _log.error("event=reviews_failed url=%s error=%s", item["url"][:90], exc)
        return data

    async def _crawl_variant_details(self, ctx, parent_product_id: str | None,
                                     seed_combinations: list[dict]) -> list[dict]:
        """Breadth-first crawl of this product's OTHER real SKUs, each
        visited in a new tab of the SAME browser context (never a new
        browser), reusing ``_scrape_product_page`` - the exact same price/
        specs/images/availability extraction used for the parent product,
        zero new extraction code. The frontier expands using whatever
        FURTHER sibling links each visited page itself reveals
        (``sibling_links``, captured by _scrape_product_page), not just the
        parent's own swatches: confirmed live that Flipkart's colour swatches
        only cross-link from the page you're currently viewing, while a given
        colour's OTHER storage options only appear once you're on THAT
        colour's own page - so a single-page combo scan (the old behaviour)
        silently missed 3 of a Galaxy S26 listing's 4 real colours. Bounded
        by ``variant_detail_concurrency`` (per-depth parallelism) and
        ``_VARIANT_MAX_DEPTH``/``_VARIANT_MAX_TOTAL`` (so a product with an
        unusually large combination matrix can't runaway-crawl). Reviews are
        deliberately NOT re-scraped per variant: Flipkart's own real
        per-review variant signal ("Review for: ..."), when present, is
        matched back from the reviews already collected once at the parent
        listing (see mapper.py). One SKU's failure is isolated (recorded as
        ``variant_extraction: {"status": "failed", ...}``) and never stops
        the rest of the crawl."""
        sem = asyncio.Semaphore(max(1, self.cfg.variant_detail_concurrency))
        visited: dict[str, dict] = {}
        known_pids = {parent_product_id}
        frontier = {c["platform_variant_id"]: c for c in seed_combinations
                    if c.get("platform_variant_id") not in known_pids}
        known_pids |= set(frontier)

        async def _visit(combo: dict) -> dict:
            pid = combo["platform_variant_id"]
            attrs = dict(combo.get("attributes") or {})
            base = {
                "variant_id": common.variant_id(parent_product_id, pid),
                "parent_product_id": parent_product_id,
                "platform_variant_id": pid,
                "variant_name": ", ".join(f"{k}: {v}" for k, v in attrs.items()),
                "attributes": attrs,
                "product_url": combo["product_url"],
            }
            discovered: list[dict] = []
            async with sem:
                page = None
                try:
                    page = await ctx.new_page()
                    parsed = await self._scrape_product_page(page, {"url": combo["product_url"],
                                                                     "is_sponsored": False})
                    if parsed is None:
                        raise RuntimeError("blocked or not a product page")
                    # The variant's own specification table is the most
                    # reliable attribute source (real values read straight off
                    # that SKU's own page) - merged in alongside whatever the
                    # sibling link already told us; never overwritten with a
                    # guess, only supplemented with real evidence.
                    own_attrs = parsing.attributes_from_specs(parsed.get("specifications_flat") or {})
                    merged = {**attrs, **own_attrs}
                    base["attributes"] = merged
                    base["variant_name"] = ", ".join(f"{k}: {v}" for k, v in merged.items())
                    ratings = parsed.get("ratings") or {}
                    base.update({
                        "images": parsed.get("images", []),
                        "videos": parsed.get("videos", []),
                        "specifications": parsed.get("specifications", {}),
                        "availability": {"status": parsing.availability_status(
                            (parsed.get("price") or {}).get("availability"))},
                        "commercial": {"price": parsed.get("price"), "offers": parsed.get("offers")},
                        "metrics": {"rating": ratings.get("average_rating"),
                                    "total_ratings": ratings.get("total_ratings"),
                                    "total_reviews": ratings.get("total_reviews")},
                        "variant_extraction": {"status": "completed", "source": "platform"},
                    })
                    discovered = parsing.sibling_combos(parsed.get("sibling_links"), exclude_pid=pid)
                except Exception as exc:
                    _log.warning("event=variant_detail_failed parent=%s pid=%s error=%s",
                                 parent_product_id, pid, exc)
                    base["variant_extraction"] = {"status": "failed", "source": "platform",
                                                  "error": str(exc)[:200]}
                finally:
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
            return base, discovered

        depth = 0
        while frontier and depth < _VARIANT_MAX_DEPTH and len(visited) < _VARIANT_MAX_TOTAL:
            batch = list(frontier.values())
            frontier = {}
            for base, discovered in await asyncio.gather(*[_visit(c) for c in batch]):
                visited[base["platform_variant_id"]] = base
                if len(visited) >= _VARIANT_MAX_TOTAL:
                    continue
                for nc in discovered:
                    npid = nc["platform_variant_id"]
                    if npid not in known_pids:
                        known_pids.add(npid)
                        frontier[npid] = nc
            depth += 1
        return list(visited.values())

    async def _scrape_all_variant_details(self, ctx, data: dict) -> list[dict]:
        """Build this product's variant combinations from every sibling link
        discovered on its own page - both the generic, label-independent
        ``sibling_links`` scan (catches colour, which Flipkart's newer
        template renders with no text label at all) and the label-based
        on-page-swatch scan (kept for whatever attribute text IT does carry,
        e.g. storage/RAM pill text) - then crawls every OTHER one
        (data["product_id"]/"item_id" is already fully scraped by the
        caller), and folds the parent's own combination in using data already
        scraped above - no second visit to its own page. Isolated from the
        rest of the product: any failure here is logged and yields an empty
        variant_details list rather than failing the whole product."""
        parent_pid = data.get("product_id") or data.get("item_id")
        try:
            combos = parsing.extract_variant_combinations(data.get("variants") or {})
            by_pid = {c["platform_variant_id"]: dict(c)
                      for c in parsing.sibling_combos(data.get("sibling_links"), exclude_pid=parent_pid)}
            for c in combos:
                pid = c["platform_variant_id"]
                if pid == parent_pid:
                    continue
                entry = by_pid.setdefault(pid, {"platform_variant_id": pid,
                                                "product_url": c["product_url"], "attributes": {}})
                entry["attributes"] = {**entry.get("attributes", {}), **c.get("attributes", {})}
                entry.setdefault("product_url", c["product_url"])

            variant_details = await self._crawl_variant_details(ctx, parent_pid, list(by_pid.values()))

            own_combo = next((c for c in combos if c.get("platform_variant_id") == parent_pid), None)
            parent_spec_attrs = parsing.attributes_from_specs(data.get("specifications_flat") or {})
            parent_attrs = {**(own_combo or {}).get("attributes", {}), **parent_spec_attrs}
            if parent_attrs:
                ratings = data.get("ratings") or {}
                variant_details.insert(0, {
                    "variant_id": common.variant_id(parent_pid, parent_pid),
                    "parent_product_id": parent_pid,
                    "platform_variant_id": parent_pid,
                    "variant_name": ", ".join(f"{k}: {v}" for k, v in parent_attrs.items()),
                    "attributes": parent_attrs,
                    "product_url": data["url"],
                    "images": data.get("images", []),
                    "videos": data.get("videos", []),
                    "specifications": data.get("specifications", {}),
                    "availability": {"status": parsing.availability_status(
                        (data.get("price") or {}).get("availability"))},
                    "commercial": {"price": data.get("price"), "offers": data.get("offers")},
                    "metrics": {"rating": ratings.get("average_rating"),
                                "total_ratings": ratings.get("total_ratings"),
                                "total_reviews": ratings.get("total_reviews")},
                    "variant_extraction": {"status": "completed", "source": "platform",
                                           "note": "parent/default listing - already scraped above"},
                })
            return variant_details
        except Exception as exc:
            _log.warning("event=variant_details_failed url=%s error=%s", data.get("url"), exc)
            return []

    def _build_price(self, pr: dict, text: str, offer: dict) -> dict:
        tp = parsing.price_from_text(text)
        price_source = "dom" if pr.get("mrp") else ("text" if tp["mrp"] else "none")
        selling = parsing.to_number(pr.get("selling")) or parsing.to_number(tp["selling"]) or parsing.to_number(offer.get("price"))
        mrp = parsing.to_number(pr.get("mrp")) or parsing.to_number(tp["mrp"])
        if mrp and selling and selling > mrp:
            selling, mrp = mrp, selling
        if mrp and selling and mrp == selling:
            mrp = None
        discount = pr.get("discount") or tp["discount"]
        if not discount and selling and mrp and mrp > selling:
            discount = f"{round((mrp - selling) / mrp * 100)}%"
        # Flipkart's own "Buy at ₹X / Lowest price for you" callout - the best
        # price achievable after applying all available offers together, not
        # a separately-badged "special deal" (there's no such label on the
        # page; that was just this field's earlier, misleading name).
        wow = re.search(r"Buy at\s*₹\s*([\d,]+)", text)
        fee = re.search(r"\+\s*₹\s*([\d,]+)\s*Protect Promise Fee", text, re.I)
        extra = re.search(r"Extra\s*₹\s*([\d,]+)\s*off", text, re.I)
        return {
            "currency": offer.get("priceCurrency", "INR"),
            "selling_price": selling,
            "mrp": mrp,
            "discount_percent": discount,
            "lowest_price_with_offers": parsing.to_number(wow.group(1)) if wow else None,
            "protect_promise_fee": parsing.to_number(fee.group(1)) if fee else None,
            "availability": offer.get("availability"),
            "discount_amount": round(mrp - selling, 2) if mrp and selling and mrp > selling else None,
            "extra_discount_amount": parsing.to_number(extra.group(1)) if extra else None,
            "price_source": price_source,
        }

    def _build_ratings(self, agg: dict, text: str) -> dict:
        rating = agg.get("ratingValue")
        rating_count = agg.get("ratingCount")
        review_count = agg.get("reviewCount")
        if not rating:
            m = re.search(r"(\d\.\d)\s*★?\s*\n?\s*\|?\s*([\d,]+)", text)
            if m:
                rating, rating_count = m.group(1), m.group(2)
        m = re.search(r"([\d,]+)\s+Ratings?\s*(?:&|and)\s*([\d,]+)\s+Reviews?", text, re.I)
        if m:
            rating_count, review_count = m.group(1), m.group(2)
        return {
            "average_rating": parsing.to_number(rating),
            "total_ratings": parsing.to_number(rating_count),
            "total_reviews": parsing.to_number(review_count),
            "breakdown": parsing.parse_breakdown(text),
        }

    def _build_offers(self, text: str) -> dict:
        end_all = [r"^EMI offers", r"^Instant loan offers", r"^Apply for", r"^Bank offers",
                   r"^Exchange offer", r"^Additional benefits", r"^Delivery details"]
        bank_blk = parsing.section(text, r"^Bank offers\s*$", [e for e in end_all if "Bank" not in e])
        emi_blk = parsing.section(text, r"^EMI offers\s*$", [e for e in end_all if "EMI" not in e])
        loan_blk = parsing.section(text, r"^Instant loan offers\s*$", [e for e in end_all if "loan" not in e])
        exch_blk = parsing.section(text, r"^Exchange offer\s*$", end_all[:3] + [r"^Bank offers"])
        old_style = re.findall(r"^(?:Bank Offer|Partner Offer|Special Price)\s*(.+)$", text, re.I | re.M)
        offers = {
            "bank_offers": parsing.group_offers(bank_blk, r"^₹[\d,]+\s*off|^\d+%", min_segments=2) or old_style,
            "emi_offers": parsing.group_offers(emi_blk, r"^₹[\d,]+/m", min_segments=2),
            "instant_loan_offers": parsing.group_offers(loan_blk, r"^₹[\d,]+/m", min_segments=2),
            # Dedup + drop section/button chrome ("Exchange offer" header,
            # "Apply"/"Change pincode..." buttons) the same way bank offers
            # needed it - confirmed live the block can repeat its own amount
            # line twice around that chrome (e.g. "Not available at this
            # Pincode | Up to X | Change pincode to exchange item | Exchange
            # offer | Up to X").
            "exchange_offer": " | ".join(dict.fromkeys(
                l.strip() for l in exch_blk.splitlines()
                if l.strip() and l.strip().lower() not in
                ("apply", "exchange offer", "change pincode to exchange item", "know more")
            )),
            # Each real benefit starts with "Unlock ..." - splitting on that
            # (rather than on blank lines, which this block doesn't have)
            # gives group_offers' own dedup a real boundary to work with;
            # confirmed live the page can render the same benefit tile twice,
            # which previously surfaced as one offer string with the same
            # sentence repeated back to back.
            "additional_benefits": parsing.group_offers(
                parsing.section(text, r"^Additional benefits on this purchase\s*$", [r"^Delivery details"]),
                r"^Unlock\b"),
        }
        offers["no_cost_emi_mentioned"] = bool(re.search(r"no[\s-]*cost\s*emi", text, re.I))
        offers["coupon_mentioned"] = bool(re.search(r"\bcoupon\b", text, re.I))
        return offers

    async def _fill_emi_offers(self, page: Page, offers: dict) -> None:
        """Flipkart renders EMI/instant-loan tiles in the SAME panel as bank
        offers, swapped in only after "View EMI offers" is clicked (confirmed
        live: clicking it replaces the "Bank offers" header/tiles in place,
        it does not add a separate section) - which is why emi_offers and
        instant_loan_offers came back empty even on products that genuinely
        have EMI plans. _build_offers() already ran against the pre-click
        text (so bank_offers is correct); here we toggle to the EMI view,
        parse that text for EMI/instant-loan tiles only, then toggle back so
        the page is left in its original state for the rest of scraping."""
        try:
            toggle = page.get_by_text(re.compile(r"^View EMI offers$", re.I))
            if not await toggle.count():
                return
            el = toggle.first
            if not await el.is_visible():
                return
            await el.scroll_into_view_if_needed(timeout=1500)
            await el.click(timeout=2000)
            await page.wait_for_timeout(500)
            emi_text = (await self._inner_text(page)).split("Digital plans with this product")[0]
            fresh = self._build_offers(emi_text)
            offers["emi_offers"] = fresh["emi_offers"]
            offers["instant_loan_offers"] = fresh["instant_loan_offers"]
            back = page.get_by_text(re.compile(r"^View non-EMI offers$", re.I))
            if await back.count():
                await back.first.click(timeout=2000)
                await page.wait_for_timeout(300)
        except Exception as exc:
            _log.warning("event=emi_offers_fetch_failed error=%s", exc)

    def _build_logistics(self, text: str):
        deliv = re.search(r"Delivery by\s*([^\n]+)", text)
        seller = re.search(r"Seller:\s*([^\n]+)\n?\s*([\d.]+)\s*★?", text)
        warranty_lines = list(dict.fromkeys(re.findall(r"^.*\bwarranty\b.*$", text, re.I | re.M)))[:8]
        addr = re.search(r"Delivery details\s*\n(.+)", text)
        delivery = {
            "estimate": deliv.group(1).strip() if deliv else None,
            "address_line": addr.group(1).strip() if addr else None,
            "free_delivery": bool(re.search(r"Free\s*Delivery", text, re.I)),
        }
        seller_info = {
            "name": seller.group(1).strip() if seller else None,
            "rating": parsing.to_number(seller.group(2)) if seller else None,
        }
        policies = {
            "warranty": [w.strip() for w in warranty_lines],
            "return_or_replacement": re.findall(r"\d+\s*Days?\s*(?:Replacement|Return)[^\n]*", text, re.I)[:2],
            "cash_on_delivery": bool(re.search(r"Cash on\s*Delivery", text, re.I)),
            "flipkart_assured": bool(re.search(r"Flipkart\s*Assured", text, re.I)),
        }
        return delivery, seller_info, policies

    async def _build_specs(self, page: Page, text: str) -> dict:
        specs = await self._ev(page, scripts.JS_SPECS_TABLES, {}) or {}
        source = "table" if specs else "text"
        block = await self._ev(page, scripts.JS_SPECS_TEXT, "") or ""
        if not block and not specs:
            block = parsing.section(
                text, r"^Specifications\s*$",
                [r"^Ratings & Reviews", r"^Questions and Answers", r"^Safe and Secure"])
        if block:
            text_specs = parsing.parse_spec_text(block)
            if not specs:
                specs = text_specs
            else:
                # The structural (JS_SPECS_TABLES) result above never reaches
                # a legal/manufacturer-info block Flipkart renders in a
                # separate modal dialog (opened by _expand_sections clicking
                # "Manufacturer Info") - confirmed live: that modal has no
                # content sibling adjacent to its own header to walk into.
                # The text-based extraction reliably captures it instead, so
                # merge in any section the structural pass did not already
                # find for itself - supplementing, never overwriting a
                # section the structural pass already populated.
                for name, rows in text_specs.items():
                    if name not in specs:
                        specs[name] = rows
        return {
            "specifications": specs,
            "specifications_flat": parsing.flatten_specs(specs),
            "specifications_parsed_from": source,
        }

    async def _scrape_reviews(self, page: Page, product_url: str):
        review_url = parsing.build_review_url(product_url)
        await self._load_review_page(page, review_url)
        text = await self._inner_text(page)

        summary = parsing.parse_review_summary(text)
        summary["aspect_ratings"] = await self._scrape_aspects(page)
        summary["review_url"] = review_url

        return await self._scrape_all_reviews(page, review_url, summary)

    async def _scrape_all_reviews(self, page: Page, review_url: str, summary: dict):
        """Every review Flipkart lists: each sort order (the requested one
        first) is loaded page by page until its last page; reviews are
        de-duplicated across listings. Stops early only when the reported
        review count is reached or Flipkart blocks the session."""
        cfg = self.cfg
        total = summary.get("total_reviews")
        sorts = [cfg.review_sort] + [s for s in parsing.REVIEW_SORT_ORDERS if s != cfg.review_sort]
        seen: set = set()          # every review found (before verified filter)
        kept: list = []
        listings: list[dict] = []
        pages_done = 0
        blocked = False
        stop_reason = "all sort orders loaded to their last page"

        for sort in sorts:
            if total is not None and len(seen) >= total:
                stop_reason = "collected all reported reviews"
                break
            base = parsing.review_listing_url(review_url, sort)
            info = {"sort": sort, "pages": 0, "found": 0, "new": 0, "stop": None, "failed": False}
            listings.append(info)
            for p in itertools.count(1):
                url = parsing.with_page(base, p)
                page_text = await self._open_review_page(page, url)
                if page_text is None:
                    info["failed"], info["stop"] = True, f"redirected to login on page {p}"
                    self.review_listings_failed += 1
                    break
                if _BLOCK_RE.search(page_text):
                    blocked, info["stop"] = True, f"blocked on page {p}"
                    break
                got = parsing.parse_reviews(page_text)
                if not got:
                    await page.wait_for_timeout(3000)
                    got = parsing.parse_reviews(await self._inner_text(page))
                if not got:
                    info["stop"] = f"last page {p - 1}"
                    break
                info["pages"] = p
                pages_done += 1
                await self._attach_review_media(page, got)
                for r in got:
                    k = parsing.review_key(r)
                    info["found"] += 1
                    if k in seen:
                        continue
                    seen.add(k)
                    info["new"] += 1
                    if cfg.verified_purchase_only and not r.get("verified_purchase"):
                        continue
                    kept.append(r)
                if total is not None and len(seen) >= total:
                    info["stop"] = "collected all reported reviews"
                    break
                # Early-stop by date: only safe on MOST_RECENT (newest-first);
                # other sorts are not date-ordered and must run to their natural
                # end to avoid missing a recent review buried after an old one.
                if sort == "recent" and cfg.review_window_start is not None:
                    page_dates = [date.fromisoformat(r["posted_date"]) for r in got
                                 if r.get("posted_date")]
                    if page_dates and all(d < cfg.review_window_start for d in page_dates):
                        info["stop"] = f"reached date window (newest-first, page {p})"
                        break
                await asyncio.sleep(random.uniform(1.0, 2.0))
            _log.info("event=review_listing_done sort=%s pages=%d found=%d new=%d total_unique=%d stop=%s",
                      sort, info["pages"], info["found"], info["new"], len(seen), info["stop"])
            if blocked:
                stop_reason = info["stop"]
                break

        coverage = parsing.review_coverage(total, len(seen), listings, blocked)
        if coverage["complete"] is False:
            self.reviews_incomplete += 1
            _log.warning("event=reviews_incomplete url=%s available=%s extracted=%d reason=%s",
                         review_url[:90], total, len(seen), coverage["incomplete_reason"])
        summary.update({
            "pages_scraped": pages_done,
            "stop_reason": stop_reason,
            "sort_used": cfg.review_sort,
            **coverage,
            "collection_mode": "all_sort_orders",
            "listing_cap": parsing.LISTING_CAP,
            "listings_scraped": len(listings),
            "failed_listings": [l["sort"] for l in listings if l["failed"]],
            "listings": listings,
        })
        return summary, kept

    async def _attach_review_media(self, page: Page, reviews: list) -> None:
        """Extract per-review images/videos from the current review page's DOM
        cards and merge them into ``reviews`` (same page) in place. Failures are
        logged with counts so missing media is never silent."""
        if not reviews:
            return
        try:
            cards = await page.evaluate(scripts.JS_REVIEW_MEDIA)
        except Exception as exc:
            _log.warning("event=review_media_eval_failed error=%s", exc)
            return
        cards = cards or []
        if not cards:
            return
        imgs, vids, matched = parsing.attach_review_media(reviews, cards)
        if matched < len(cards):
            _log.warning(
                "event=review_media_partial cards_with_media=%d matched=%d images=%d videos=%d",
                len(cards), matched, imgs, vids)
        else:
            _log.info("event=review_media cards_with_media=%d matched=%d images=%d videos=%d",
                      len(cards), matched, imgs, vids)

    async def _open_review_page(self, page: Page, url: str) -> str | None:
        """Load a review page, retrying with back-off when Flipkart redirects
        it to /login (intermittent; the same page loads on retry). Returns the
        page text, or None when every attempt was redirected."""
        for attempt in range(self.cfg.review_retries + 1):
            try:
                await self._load_review_page(page, url)
            except Exception as exc:  # aborted navigation = redirect in flight
                _log.debug("event=review_page_goto_error url=%s error=%s", url[-60:], exc)
                await page.wait_for_timeout(1500)
            if not parsing.is_login_redirect(page.url):
                return await self._inner_text(page)
            if attempt < self.cfg.review_retries:
                delay = self.cfg.review_retry_backoff * (2 ** attempt)
                _log.info("event=review_login_redirect url=%s retry_in=%.0fs", url[-60:], delay)
                await asyncio.sleep(delay)
        _log.warning("event=review_page_login_required url=%s", url[-60:])
        return None

    async def _apply_sort(self, page: Page, review_url: str) -> str:
        sort = self.cfg.review_sort
        if sort == "helpful":
            return review_url
        chip = _REVIEW_SORT_CHIP.get(sort)
        if chip:
            try:
                await page.get_by_text(chip, exact=True).first.click(timeout=4000)
                await page.wait_for_timeout(2000)
                if "sortOrder" in page.url:
                    return page.url.split("&page=")[0]
            except Exception:
                pass
        param = _REVIEW_SORT_PARAM.get(sort)
        return f"{review_url}&sortOrder={param}" if param else review_url

    async def _scrape_aspects(self, page: Page) -> dict:
        chips = await self._ev(page, scripts.JS_ASPECT_CHIPS, []) or []
        aspects: dict = {}
        for chip in chips:
            label = chip.split("\n")[0].strip()
            name = parsing.clean(re.sub(r"[\d.★]+", "", label))
            if not name or name.lower() == "overall":
                continue
            try:
                await page.get_by_text(label, exact=True).first.click(timeout=3000)
            except Exception:
                aspects[name] = {"rating": None, "positive_feedback_percent": None, "negative_feedback_percent": None}
                continue
            head = ""
            for _ in range(3):
                await page.wait_for_timeout(1000)
                head = (await self._inner_text(page)).split("User reviews sorted by")[0]
                if re.search(r"positive\s+feedback", head, re.I):
                    break
            rating, pos, neg = parsing.parse_aspect_head(head, chips)
            aspects[name] = {"rating": rating, "positive_feedback_percent": pos, "negative_feedback_percent": neg}
        return aspects

    async def _load_review_page(self, page: Page, url: str, wait: int = 1800) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        await page.wait_for_timeout(wait)
        await self._press_escape(page)
        await self._scroll_page(page)
        await self._ev(page, scripts.JS_CLICK_MORE)
        await page.wait_for_timeout(400)

    async def _collect_media(self, page: Page):
        imgs: list = []
        vids: list = []

        async def grab():
            m = await self._ev(page, scripts.JS_IMAGES_VIDEOS, {}) or {}
            imgs.extend(m.get("images", []))
            vids.extend(m.get("videos", []))

        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)
        await grab()
        thumbs = await self._ev(page, scripts.JS_GALLERY_THUMBS, []) or []
        for t in thumbs[:20]:
            try:
                await page.mouse.click(t["x"], t["y"])
                await page.wait_for_timeout(400)
                await grab()
            except Exception:
                pass
        await self._press_escape(page)
        return parsing.dedupe_images(imgs), list(dict.fromkeys(vids))

    async def _expand_sections(self, page: Page) -> None:
        patterns = [
            r"^Product highlights$", r"^Specifications$", r"^Read More$",
            r"^\+\s*\d+\s*more offers?$", r"^View all (offers|specifications)$",
            r"^Show more$", r"^Manufacturing,?\s*Packaging\s*(and|&)\s*Import Info$",
            r"^Manufacturer (Info|Details)$",
        ]
        for pat in patterns:
            loc = page.get_by_text(re.compile(pat, re.I))
            try:
                count = min(await loc.count(), 3)
            except Exception:
                count = 0
            for i in range(count):
                try:
                    el = loc.nth(i)
                    if not await el.is_visible():
                        continue
                    await el.scroll_into_view_if_needed(timeout=1500)
                    await el.click(timeout=1500)
                    await page.wait_for_timeout(350)
                except Exception:
                    pass

    async def _scroll_page(self, page: Page) -> None:
        try:
            height = await page.evaluate("document.body.scrollHeight")
            y = 0
            for _ in range(40):
                if y >= height:
                    break
                await page.mouse.wheel(0, 900)
                await page.wait_for_timeout(180)
                y += 900
                height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

    async def _dismiss_login(self, page: Page) -> None:
        try:
            await page.get_by_text("✕").first.click(timeout=1500)
        except Exception:
            await self._press_escape(page)

    async def _press_escape(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _inner_text(self, page: Page) -> str:
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    async def _first_text(self, page: Page, selector: str):
        try:
            return (await page.locator(selector).first.inner_text()).strip()
        except Exception:
            return None

    async def _ev(self, page: Page, script: str, default=None):
        try:
            return await page.evaluate(script)
        except Exception as exc:
            _log.warning("event=evaluate_failed error=%s", exc)
            return default
