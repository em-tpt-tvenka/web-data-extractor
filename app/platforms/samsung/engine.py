from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from app.core.logging import get_logger
from app.platforms import common
from app.platforms.browser import proxy_settings, random_user_agent
from app.platforms.browser_launch import with_chrome_fallback
from app.platforms.samsung import parsing

_log = get_logger("samsung.engine")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""
_BLOCK_RE = re.compile(
    r"access denied|are you a human|unusual traffic|verify you are human"
    r"|pardon our interruption|request unsuccessful|access to this page has been denied",
    re.I,
)
# Samsung's own error page title ("error | Samsung India") when a /buy/ slug
# does not resolve to a real product (HTTP 404).
_ERROR_TITLE_RE = re.compile(r"^error\b", re.I)


@dataclass
class EngineConfig:
    search_text: str
    base_url: str = "https://www.samsung.com/in"
    category: str | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    max_pages: int = 1
    max_products: int = 20
    fetch_reviews: bool = True
    # Start of the requested date window (None = no window / collect all time).
    # Lets review pagination stop early once a confirmed newest-first page is
    # entirely older than this -- never used to skip anything within range.
    review_window_start: date | None = None
    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 60_000
    bv_api_url: str | None = None
    bv_passkey: str | None = None
    debug_dir: str | None = None
    # Bounded concurrency for independently visiting OTHER known sibling SKUs'
    # own /buy/ pages (new tab per variant, same browser context) when a real
    # URL for that SKU is known -- see ``SamsungEngine._build_variant_details``.
    variant_detail_concurrency: int = 3


class SamsungEngine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.pages_processed = 0

    async def run(self) -> list[dict]:
        cfg = self.cfg
        _log.info("event=engine_start search=%r category=%s max_products=%s headless=%s",
                  cfg.search_text, cfg.category, cfg.max_products, cfg.headless)

        if not parsing.is_brand_available_on_samsung(cfg.search_text):
            _log.info("event=brand_not_samsung query=%r result=empty", cfg.search_text)
            return []

        category = cfg.category or parsing.resolve_category(cfg.search_text)
        models: list[dict] = []
        launch_args = ["--disable-blink-features=AutomationControlled", "--start-maximized"]

        async with async_playwright() as pw:
            browser = None
            mode = "persistent_profile" if cfg.chrome_profile_dir else "fresh_context"
            _log.info("event=browser_launching mode=%s channel=%s headless=%s", mode, cfg.browser_channel, cfg.headless)

            async def _do_launch():
                if cfg.chrome_profile_dir:
                    return await pw.chromium.launch_persistent_context(
                        cfg.chrome_profile_dir, channel=cfg.browser_channel, headless=cfg.headless,
                        args=launch_args, user_agent=random_user_agent(UA), locale="en-IN",
                        viewport={"width": 1440, "height": 900}, proxy=proxy_settings())
                browser_ = await pw.chromium.launch(headless=cfg.headless, channel=cfg.browser_channel,
                                                   args=launch_args, proxy=proxy_settings())
                return await browser_.new_context(user_agent=random_user_agent(UA), locale="en-IN", viewport={"width": 1440, "height": 900})

            try:
                ctx = await with_chrome_fallback(
                    _do_launch, platform="samsung", pw=pw, profile_dir=cfg.chrome_profile_dir,
                    headless=cfg.headless, user_agent=random_user_agent(UA), locale="en-IN",
                    proxy=proxy_settings(), viewport={"width": 1440, "height": 900}, extra_args=launch_args,
                )
            except Exception as exc:
                _log.error("event=browser_launch_failed error=%s", exc)
                raise
            await ctx.add_init_script(_STEALTH)
            _log.info("event=browser_launched mode=%s", mode)

            try:
                urls = await self._discover(ctx, category)
                _log.info("event=discovery_done urls=%d", len(urls))
                for url in urls:
                    if cfg.max_products > 0 and len(models) >= cfg.max_products:
                        break
                    page = await ctx.new_page()
                    try:
                        model = await self._scrape_product(ctx, page, url)
                        if model and model.get("product_name"):
                            models.append(model)
                            _log.info("event=product_done id=%s reviews=%d",
                                      model.get("product_id"), len(model.get("reviews", [])))
                    except Exception as exc:
                        _log.error("event=product_failed url=%s error=%s", url[:90], exc)
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

    async def _discover(self, ctx, category: str | None) -> list[str]:
        cfg = self.cfg
        base = cfg.base_url.rstrip("/")
        excludes = [k.lower() for k in cfg.exclude_keywords if k]
        found: list[str] = []
        seen: set[str] = set()

        def add(url: str):
            # Samsung's search/sitemap surface deep variant URLs that redirect to
            # the series landing page; map every candidate to its canonical
            # non-redirecting /buy/ PDP and de-duplicate on that.
            url = parsing.canonical_buy_url(url, base)
            low = url.lower()
            if url in seen:
                return
            if excludes and any(k in low for k in excludes):
                return
            seen.add(url)
            found.append(url)

        page = await ctx.new_page()
        try:
            search_url = f"{base}/aisearch/?searchvalue={quote_plus(cfg.search_text)}"
            _log.info("event=search url=%s", search_url[:110])
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                await page.wait_for_timeout(2500)
                await self._dismiss(page)
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, 800)")
                    await page.wait_for_timeout(900)
                html = await self._html(page)
                self.pages_processed += 1
                # if not _BLOCK_RE.search(html):
                #     for u in parsing.search_result_urls(html, page.url, base):
                urls = parsing.search_result_urls(html, page.url, base)
                for u in urls:
                        add(u)
                        if cfg.max_products > 0 and len(found) >= cfg.max_products:
                            break
            except Exception as exc:
                _log.warning("event=search_failed error=%s", exc)
            _log.info("event=search_urls count=%d", len(found))
        finally:
            try:
                await page.close()
            except Exception:
                pass

        if cfg.max_products <= 0 or len(found) < cfg.max_products:
            for sm in parsing.sitemaps_for_category(category, base):
                try:
                    resp = await ctx.request.get(sm, timeout=cfg.nav_timeout_ms)
                    if not resp.ok:
                        continue
                    xml = await resp.text()
                    sitemap_urls = parsing.product_urls_from_sitemap(xml, base, category)
                    _log.info("event=sitemap url=%s found=%d", sm, len(sitemap_urls))
                    terms = [t for t in re.findall(r"[a-z0-9]+", cfg.search_text.lower()) if len(t) > 1]
                    ranked = sorted(
                        sitemap_urls,
                        key=lambda u: -sum(1 for t in terms if t in u.lower()))
                    for u in ranked:
                        add(u)
                        if cfg.max_products > 0 and len(found) >= cfg.max_products:
                            break
                except Exception as exc:
                    _log.warning("event=sitemap_failed url=%s error=%s", sm, exc)
                if cfg.max_products > 0 and len(found) >= cfg.max_products:
                    break
        return found

    def _looks_failed(self, model: dict | None, status: int | None) -> bool:
        """True when a /buy/ navigation did not land on a real product page:
        a non-2xx status, or Samsung's own "error | Samsung India" error page
        (seen on slugs its URL router does not recognize)."""
        if status is not None and status >= 400:
            return True
        name = (model or {}).get("product_name") or ""
        return not name.strip() or bool(_ERROR_TITLE_RE.match(name.strip()))

    async def _scrape_product(self, ctx, page: Page, url: str) -> dict | None:
        cfg = self.cfg
        # Capture Bazaarvoice's own reviews.json request as the review widget
        # loads: that URL carries the passkey + ProductId, so reviews work
        # without any pre-configured passkey.
        bv_urls: list[str] = []

        def _on_req(r):
            u = r.url
            if "bazaarvoice" in u and "reviews.json" in u:
                bv_urls.append(u)

        page.on("request", _on_req)
        try:
            # The full deep slug + /buy/ resolves correctly for most lines
            # (Samsung normalizes it server-side), but 404s for some (e.g.
            # Galaxy A-series marketing colour slugs like "awesome-icyblue"
            # its router does not recognize). Try it first, then fall back to
            # progressively shorter candidates, validating each live rather
            # than guessing which slug tokens are "real" model name vs filler.
            candidates = [url] + parsing.buy_url_fallback_candidates(url, cfg.base_url)
            model = None
            for attempt, candidate in enumerate(candidates):
                try:
                    resp = await page.goto(candidate, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                except Exception as exc:
                    _log.warning("event=buy_url_nav_failed url=%s error=%s", candidate[:100], exc)
                    continue
                await page.wait_for_timeout(2000)
                await self._dismiss(page)
                await self._expand(page)
                # Scroll further so the (lazy) reviews widget enters view and
                # fires its reviews.json request.
                for _ in range(10):
                    await page.evaluate("window.scrollBy(0, 1200)")
                    await page.wait_for_timeout(450)
                await page.wait_for_timeout(1500)
                html = await self._html(page)
                candidate_model = parsing.parse_product(html, page.url)
                status = resp.status if resp else None
                if not self._looks_failed(candidate_model, status):
                    model = candidate_model
                    if attempt > 0:
                        _log.info("event=buy_url_fallback_resolved original=%s resolved=%s attempt=%d",
                                  url[:90], candidate[:90], attempt)
                    break
                _log.warning("event=buy_url_candidate_failed url=%s status=%s title=%r attempt=%d/%d",
                            candidate[:100], status, (candidate_model or {}).get("product_name"),
                            attempt, len(candidates) - 1)
            if model is None:
                _log.error("event=all_buy_url_candidates_failed url=%s candidates_tried=%d",
                          url[:100], len(candidates))
                return None

            # The commercial '/buy/' page just scraped carries no full
            # specification table (confirmed live: only a short "Features"/
            # "Review" tab pair). The real spec table lives on the per-SKU
            # overview page (same URL with '/buy/' dropped) and/or, for lines
            # whose overview page is itself a visual/marketing page with no
            # spec table (confirmed on Galaxy S flagships), on a separate
            # family-level '/specs/' page that overview page links to.
            # Fetch both, re-parse with the richer specs merged in, and keep
            # that result; fall back to the '/buy/'-only model on any failure.
            resolved_url = page.url
            extra_spec_html: list[str] = []
            try:
                ov_url = parsing.overview_url(resolved_url)
                await page.goto(ov_url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                await page.wait_for_timeout(1500)
                await self._dismiss(page)
                await self._expand(page)
                for _ in range(6):
                    await page.evaluate("window.scrollBy(0, 1200)")
                    await page.wait_for_timeout(350)
                ov_html = await self._html(page)
                extra_spec_html.append(ov_html)
                specs_url = parsing.find_specs_page_url(ov_html, page.url)
                if specs_url:
                    await page.goto(specs_url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                    await page.wait_for_timeout(1500)
                    await self._expand(page)
                    extra_spec_html.append(await self._html(page))
            except Exception as exc:
                _log.warning("event=spec_pages_fetch_failed url=%s error=%s", resolved_url[:100], exc)
            if extra_spec_html:
                enriched = parsing.parse_product(html, resolved_url, extra_spec_html=extra_spec_html)
                if enriched.get("product_name"):
                    model = enriched

            if model.get("product_name") and cfg.fetch_reviews:
                jsonld = model.get("reviews") or []
                bv, summary = await self._bazaarvoice(
                    ctx,
                    product_id=model.get("bv_product_id") or model.get("product_id"),
                    captured_urls=list(bv_urls),
                    passkey=model.get("bv_passkey"),
                    api_url=model.get("bv_api_url"),
                )
                model["reviews"] = parsing.merge_reviews(jsonld, bv)
                if summary:
                    model["review_summary"] = summary
                    ratings = model.get("ratings") or {}
                    if summary.get("average_rating") is not None:
                        ratings["average_rating"] = summary["average_rating"]
                    if summary.get("total_reviews") is not None:
                        ratings["total_reviews"] = summary["total_reviews"]
                        ratings["total_ratings"] = summary.get("total_ratings") or summary["total_reviews"]
                    if summary.get("rating_breakdown"):
                        ratings["breakdown"] = summary["rating_breakdown"]
                    model["ratings"] = ratings

            if model.get("product_name"):
                try:
                    model["variant_details"] = await self._build_variant_details(ctx, model)
                except Exception as exc:
                    _log.warning("event=variant_details_failed url=%s error=%s", resolved_url[:100], exc)
                    model["variant_details"] = []
            # Internal BV handles are not part of the normalized output.
            for k in ("bv_passkey", "bv_api_url", "bv_product_id"):
                model.pop(k, None)
            return model
        finally:
            try:
                page.remove_listener("request", _on_req)
            except Exception:
                pass

    async def _bazaarvoice(self, ctx, product_id: str | None, captured_urls: list[str] | None = None,
                           passkey: str | None = None, api_url: str | None = None):
        """Fetch ALL Bazaarvoice reviews for a product, paginating by Offset, and
        return ``(reviews, review_summary)``.

        Base URL precedence: the passkey + ProductId read from the page's hidden
        inputs (most reliable), then a reviews.json URL the widget itself
        requested, then the configured bv_api_url + bv_passkey."""
        from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
        cfg = self.cfg
        base = None
        pk = passkey or cfg.bv_passkey
        api = (api_url or cfg.bv_api_url or "https://api.bazaarvoice.com/data").rstrip("/")
        if pk and product_id:
            base = (f"{api}/reviews.json?apiversion=5.4&passkey={pk}"
                    f"&Filter=ProductId:{product_id}&Include=Products&Stats=Reviews")
        elif captured_urls:
            base = captured_urls[-1]
        if not base:
            _log.info("event=bazaarvoice_skip id=%s reason=no_passkey_or_captured_url", product_id)
            return [], None
        sp = urlsplit(base)
        q = {k: v[0] for k, v in parse_qs(sp.query).items()}
        q.setdefault("apiversion", "5.4")
        q.setdefault("Include", "Products")
        q.setdefault("Stats", "Reviews")
        q["Limit"] = "100"
        q.setdefault("Sort", "SubmissionTime:desc")
        # Early-stop by date is only valid when this request is CONFIRMED sorted
        # newest-first -- a captured widget URL may carry its own different
        # Sort value, so check the value actually in effect, never assume it.
        date_sorted = q.get("Sort", "").strip().lower() == "submissiontime:desc"
        out: list[dict] = []
        summary = None
        offset, total, pages = 0, None, 0
        stop_reason = None
        # Each BV page holds up to 100 reviews; always collect everything
        # (hard safety ceiling of 60 pages = 6000 reviews).
        max_pages = 60
        while pages < max_pages:
            q["Offset"] = str(offset)
            page_url = urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q, safe=":,"), ""))
            try:
                resp = await ctx.request.get(page_url, timeout=cfg.nav_timeout_ms)
                if not resp.ok:
                    _log.warning("event=bazaarvoice_http id=%s offset=%d status=%s", product_id, offset, resp.status)
                    break
                data = await resp.json()
            except Exception as exc:
                _log.warning("event=bazaarvoice_failed id=%s offset=%d error=%s", product_id, offset, exc)
                break
            if summary is None:
                summary = parsing.bazaarvoice_summary(data)
            batch = parsing.parse_bazaarvoice(data, product_id)
            out.extend(batch)
            if isinstance(data, dict):
                total = data.get("TotalResults")
                got = len(data.get("Results") or [])
            else:
                got = 0
            pages += 1
            if got == 0 or (total is not None and len(out) >= total):
                break
            if date_sorted and cfg.review_window_start is not None and batch:
                batch_dates = [date.fromisoformat(r["posted_date"]) for r in batch
                              if r.get("posted_date")]
                # Only trust the signal when every dated review in this page is
                # parseable and all are before the window: with a confirmed
                # newest-first sort, nothing on a later page can be newer, so
                # anything further is guaranteed outside the requested window.
                if batch_dates and all(d < cfg.review_window_start for d in batch_dates):
                    stop_reason = "reached date window (newest-first)"
                    break
            offset += 100
        _log.info("event=bazaarvoice_done id=%s collected=%d total=%s pages=%d stop=%s",
                  product_id, len(out), total, pages, stop_reason or "natural_end")
        return out, summary

    def _variant_attrs(self, option: dict) -> dict:
        return {k: v for k, v in (
            ("Colour", option.get("color")), ("Storage", option.get("storage")),
            ("RAM", option.get("memory")),
        ) if v}

    async def _build_variant_details(self, ctx, model: dict) -> list[dict]:
        """``variant_details`` for this discovered SKU's own record: its own
        (already-scraped) entry plus one entry per OTHER known sibling SKU in
        the same colour/storage family.

        Samsung's variant family is read from either the ld+json
        ``ProductGroup`` (which carries each sibling's own real page URL) or,
        when that is absent (confirmed live on Galaxy A-series), from the
        hidden ``BV-buyingOptionData`` map (colour/storage/SKU only, no URL --
        see ``parsing._variants_from_buying_option_data``). A sibling with a
        real URL is independently visited (new tab, same browser context,
        bounded concurrency, isolated failure) exactly like the current page
        was; a sibling with no URL is honestly reported as ``not_visited``
        with its known colour/storage/SKU, never a fabricated price."""
        variants = model.get("variants") or {}
        opts = [o for lst in variants.values() if isinstance(lst, list) for o in lst]
        if not opts:
            return []
        parent_id = model.get("product_id")
        current_opt, others = parsing.split_variant_options(variants, model.get("sku"))
        details: list[dict] = []
        if current_opt is not None:
            attrs = self._variant_attrs(current_opt)
            ratings = model.get("ratings") or {}
            current_sku = current_opt.get("product_id") or model.get("sku") or parent_id
            details.append({
                "variant_id": common.variant_id(parent_id, current_sku),
                "parent_product_id": parent_id,
                "platform_variant_id": current_sku,
                "variant_name": current_opt.get("option")
                or ", ".join(f"{k}: {v}" for k, v in attrs.items()) or parent_id,
                "attributes": attrs,
                "product_url": model.get("url"),
                "images": model.get("images", []),
                "videos": model.get("videos", []),
                "specifications": model.get("specifications", {}),
                "availability": (model.get("price") or {}).get("availability"),
                "commercial": {
                    "price": model.get("price") or {},
                    "offers": model.get("offers") or {},
                    "delivery": model.get("delivery") or {},
                },
                "metrics": {
                    "rating": ratings.get("average_rating"),
                    "total_ratings": ratings.get("total_ratings"),
                    "total_reviews": ratings.get("total_reviews"),
                },
                "variant_extraction": {
                    "status": "completed", "source": "platform",
                    "note": "current/selected SKU for this discovered URL - already scraped above",
                },
            })
        if not others:
            return details
        sem = asyncio.Semaphore(max(1, self.cfg.variant_detail_concurrency))

        parent_url = model.get("url")

        async def _one(option: dict) -> dict:
            async with sem:
                page = await ctx.new_page()
                try:
                    return await self._scrape_sibling_variant(page, parent_id, parent_url, option)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

        results = await asyncio.gather(*[_one(o) for o in others])
        details.extend(results)
        return details

    async def _scrape_sibling_variant(self, page: Page, parent_id: str | None, parent_url: str | None,
                                      option: dict) -> dict:
        """One OTHER sibling SKU's own ``variant_details`` entry: independently
        visited and parsed when a real, confirmed-working URL can be derived
        (see ``parsing.sibling_variant_url``), else honestly marked
        ``not_visited`` -- never a guessed/fabricated price."""
        cfg = self.cfg
        sku = option.get("product_id")
        attrs = self._variant_attrs(option)
        base = {
            "variant_id": common.variant_id(parent_id, sku),
            "parent_product_id": parent_id,
            "platform_variant_id": sku,
            "variant_name": option.get("option") or ", ".join(f"{k}: {v}" for k, v in attrs.items()) or sku,
            "attributes": attrs,
            "product_url": option.get("url"),
        }
        url = parsing.sibling_variant_url(option.get("url"), sku, parent_url, cfg.base_url)
        if not url:
            base["variant_extraction"] = {
                "status": "not_visited", "source": "platform",
                "reason": ("no independently-visitable URL could be confirmed for this SKU from the "
                           "source data (BV-buyingOptionData carries colour/storage/SKU but no page URL, "
                           "and guessing a marketing-slug URL from the SKU alone is unreliable); its "
                           "price and availability are therefore not reported rather than guessed"),
            }
            return base
        base["product_url"] = url
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
            await page.wait_for_timeout(1500)
            await self._dismiss(page)
            await self._expand(page)
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, 1200)")
                await page.wait_for_timeout(350)
            await page.wait_for_timeout(800)
            html = await self._html(page)
            status = resp.status if resp else None
            parsed = parsing.parse_product(html, page.url)
            if self._looks_failed(parsed, status):
                raise ValueError(f"sibling buy page did not resolve to a real product (status={status})")
            price = parsed.get("price") or {}
            ratings = parsed.get("ratings") or {}
            base.update({
                "images": parsed.get("images", []),
                "videos": parsed.get("videos", []),
                "specifications": parsed.get("specifications", {}),
                "availability": price.get("availability"),
                "commercial": {
                    "price": price,
                    "offers": parsed.get("offers") or {},
                    "delivery": {"free_delivery": True} if (parsed.get("offers") or {}).get("free_delivery") else {},
                },
                "metrics": {
                    "rating": ratings.get("average_rating"),
                    "total_reviews": ratings.get("total_reviews"),
                },
                "variant_extraction": {
                    "status": "completed", "source": "platform",
                    "note": ("fetched from its own /buy/ page only - the overview/family-specs pages are "
                             "not re-fetched per sibling, to bound execution time, so its specifications "
                             "may be less complete than the parent SKU's own"),
                },
            })
        except Exception as exc:
            _log.warning("event=sibling_variant_failed parent=%s sku=%s url=%s error=%s",
                        parent_id, sku, url[:90], exc)
            base["variant_extraction"] = {"status": "failed", "source": "platform", "error": str(exc)[:200]}
        return base

    async def _expand(self, page: Page) -> None:
        for sel in (
            "button:has-text('Expand all')",
            ".pdd32-product-spec__expand-cta button",
            "button:has-text('See all specifications')",
            "button:has-text('View more')",
        ):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(500)
            except Exception:
                pass

    async def _dismiss(self, page: Page) -> None:
        for sel in ("#truste-consent-button", "button:has-text('Accept All')", "button[aria-label='Close']"):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _html(self, page: Page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""
