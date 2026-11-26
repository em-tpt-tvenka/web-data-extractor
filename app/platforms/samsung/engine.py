from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from app.core.logging import get_logger
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
    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 60_000
    bv_api_url: str | None = None
    bv_passkey: str | None = None
    debug_dir: str | None = None


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
        out: list[dict] = []
        summary = None
        offset, total, pages = 0, None, 0
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
            offset += 100
        _log.info("event=bazaarvoice_done id=%s collected=%d total=%s pages=%d",
                  product_id, len(out), total, pages)
        return out, summary

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
