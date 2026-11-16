
from __future__ import annotations

import asyncio
import random
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from app.core.logging import get_logger
from app.platforms.amazon import parsing
from app.platforms.amazon.options import Marketplace

_log = get_logger("amazon.engine")

_SORT_MAP = {
    "relevance": None,  # Amazon's default ("relevanceblender")
    "price_asc": "price-asc-rank",
    "price_desc": "price-desc-rank",
    "rating": "review-rank",
    "newest": "date-desc-rank",
}
_RESULT_SEL = "div[data-component-type='s-search-result']"
_EXPANDER_SELS = (
    "#productDetails_expanderTables_depthLeftSections a.a-expander-header[aria-expanded='false']",
    "#productDetails_expanderTables_depthRightSections a.a-expander-header[aria-expanded='false']",
    "#prodDetails a.a-expander-header[aria-expanded='false']",
)


class BlockedError(Exception):
    """CAPTCHA could not be cleared (headless, or nobody solved it in time)."""


class PageLoadError(Exception):
    """A page failed to load after all retries."""


class BrowserClosedError(Exception):
    """The browser/context went away (closed window, crash). Not retryable."""


def _is_closed_error(exc: Exception) -> bool:
    return "has been closed" in str(exc) or exc.__class__.__name__ == "TargetClosedError"


@dataclass
class AmazonEngineConfig:
    marketplace: Marketplace
    search_text: str
    sort_by: str = "relevance"
    min_price: float | None = None
    max_price: float | None = None
    include_sponsored: bool = False
    exclude_keywords: list[str] = field(default_factory=list)

    max_pages: int = 1
    max_products: int = 20
    review_pages: int = 1
    review_sort: str = "recent"
    verified_reviews_only: bool = False

    detail_concurrency: int = 1
    max_retries: int = 3
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 4.5

    headless: bool = False
    browser_channel: str = "chrome"
    profile_dir: str | None = None
    nav_timeout_ms: int = 90_000
    captcha_wait_seconds: int = 300
    debug_dir: str | None = None


def build_search_url(cfg: AmazonEngineConfig, page: int) -> str:
    params: dict[str, str] = {"k": cfg.search_text}
    sort = _SORT_MAP.get(cfg.sort_by)
    if sort:
        params["s"] = sort
    if cfg.min_price is not None or cfg.max_price is not None:
        # p_36 is the price refinement, expressed in the currency's minor unit.
        lo = int(round(cfg.min_price * 100)) if cfg.min_price is not None else ""
        hi = int(round(cfg.max_price * 100)) if cfg.max_price is not None else ""
        params["rh"] = f"p_36:{lo}-{hi}"
    if page > 1:
        params["page"] = str(page)
    return f"{cfg.marketplace.base_url}/s?{urllib.parse.urlencode(params)}"


def build_review_url(cfg: AmazonEngineConfig, asin: str, page: int) -> str:
    params = {
        "reviewerType": "avp_only_reviews" if cfg.verified_reviews_only else "all_reviews",
        "sortBy": "helpful" if cfg.review_sort == "helpful" else "recent",
        "pageNumber": str(page),
    }
    return f"{cfg.marketplace.base_url}/product-reviews/{asin}/?{urllib.parse.urlencode(params)}"


class AmazonEngine:
    def __init__(self, cfg: AmazonEngineConfig):
        self.cfg = cfg
        self.pages_processed = 0
        self.signed_in: bool | None = None
        self.failures: list[dict] = []
        self.skipped: dict[str, int] = {"sponsored": 0, "excluded_keyword": 0, "duplicate": 0}
        self.review_login_required = 0
        self.aborted: str | None = None  # why the run stopped early, if it did
        self._closed = False
        self._nav_lock = asyncio.Lock()
        self._last_nav = 0.0

    # ── Browser helpers ──────────────────────────────────────────────────

    async def _launch(self, pw) -> BrowserContext:
        cfg, mp = self.cfg, self.cfg.marketplace
        opts = dict(
            headless=cfg.headless, locale=mp.locale, timezone_id=mp.timezone,
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": mp.accept_language},
            args=["--disable-blink-features=AutomationControlled"],
        )
        _log.info("event=browser_launching channel=%s headless=%s profile=%s",
                  cfg.browser_channel, cfg.headless, cfg.profile_dir)
        if cfg.profile_dir:
            Path(cfg.profile_dir).mkdir(parents=True, exist_ok=True)
            ctx = await pw.chromium.launch_persistent_context(
                cfg.profile_dir, channel=cfg.browser_channel, **opts)
        else:
            browser = await pw.chromium.launch(
                headless=cfg.headless, channel=cfg.browser_channel, args=opts.pop("args"))
            ctx = await browser.new_context(**{k: v for k, v in opts.items() if k != "headless"})
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        ctx.on("close", lambda *_: setattr(self, "_closed", True))
        return ctx

    async def _throttle(self) -> None:
        """Polite, randomized gap between navigations, shared across workers."""
        async with self._nav_lock:
            loop = asyncio.get_event_loop()
            gap = random.uniform(self.cfg.min_delay_seconds, self.cfg.max_delay_seconds)
            wait = gap - (loop.time() - self._last_nav)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_nav = loop.time()

    async def _is_blocked(self, page: Page) -> bool:
        if "/errors/validateCaptcha" in page.url:
            return True
        try:
            return parsing.looks_blocked(await page.content())
        except Exception:
            return False

    async def _resolve_captcha(self, page: Page) -> None:
        if not await self._is_blocked(page):
            return
        # Amazon's soft interstitial has an ordinary "Continue shopping" button.
        try:
            btn = page.locator("button:has-text('Continue shopping')").first
            if await btn.is_visible(timeout=1_500):
                await btn.click()
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)
        except Exception:
            pass
        if not await self._is_blocked(page):
            return
        if self.cfg.headless:
            raise BlockedError("CAPTCHA shown; run with a visible browser to solve it manually")
        _log.warning("event=captcha_manual_required url=%s message=solve the CAPTCHA in the browser window",
                     page.url[:90])
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.cfg.captcha_wait_seconds
        while loop.time() < deadline:
            await asyncio.sleep(2)
            if not await self._is_blocked(page):
                _log.info("event=captcha_cleared")
                return
        raise BlockedError("CAPTCHA not solved in time")

    async def _scroll(self, page: Page, steps: int) -> None:
        """Scroll like a reader so lazy sections (specs, reviews) render."""
        for _ in range(steps):
            await page.mouse.wheel(0, random.randint(700, 1200))
            await asyncio.sleep(random.uniform(0.25, 0.6))
        for sel in _EXPANDER_SELS:
            try:
                for el in (await page.locator(sel).all())[:8]:
                    if await el.is_visible():
                        await el.click(timeout=1_500)
            except Exception:
                pass
        await asyncio.sleep(0.6)

    async def _goto(self, page: Page, url: str, wait_for: str | None = None,
                    scroll_steps: int = 0) -> str:
        """Load a page with retries and backoff; returns its HTML."""
        last_err: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            if self._closed or page.is_closed():
                raise BrowserClosedError("browser was closed")
            await self._throttle()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                await self._resolve_captcha(page)
                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=15_000)
                    except Exception:
                        pass
                if scroll_steps:
                    await self._scroll(page, scroll_steps)
                html = await page.content()
                if parsing.looks_blocked(html):
                    raise BlockedError("CAPTCHA page")
                return html
            except (BlockedError, BrowserClosedError):
                raise
            except Exception as exc:
                if _is_closed_error(exc) or page.is_closed():
                    raise BrowserClosedError("browser was closed") from exc
                last_err = exc
                backoff = 5 * attempt + random.uniform(0, 3)
                _log.warning("event=load_failed attempt=%d/%d retry_in=%.0fs url=%s error=%s",
                             attempt, self.cfg.max_retries, backoff, url[:100], exc.__class__.__name__)
                await asyncio.sleep(backoff)
        raise PageLoadError(f"giving up on {url}: {last_err}")

    async def _open_home(self, page: Page) -> None:
        await self._goto(page, self.cfg.marketplace.base_url + "/")
        for sel in ("#sp-cc-accept", "input[data-action-type='DISMISS']"):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1_500):
                    await loc.click()
            except Exception:
                pass
        try:
            label = await page.locator("#nav-link-accountList-nav-line-1").first.inner_text(timeout=3_000)
            self.signed_in = "sign in" not in label.lower()
        except Exception:
            self.signed_in = None
        _log.info("event=home_opened marketplace=%s signed_in=%s",
                  self.cfg.marketplace.domain, self.signed_in)

    def _save_debug(self, name: str, html: str) -> None:
        if not self.cfg.debug_dir:
            return
        try:
            d = Path(self.cfg.debug_dir) / "amazon"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{name}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

    # ── Product + reviews ────────────────────────────────────────────────

    async def _collect_review_pages(self, page: Page, asin: str) -> dict:
        out = {"reviews": [], "pages_checked": 0, "total_reviews": None, "login_required": False}
        seen: set = set()
        for n in range(1, self.cfg.review_pages + 1):
            html = await self._goto(page, build_review_url(self.cfg, asin, n),
                                    wait_for="[data-hook='review'], #ap_email, #cm_cr-review_list",
                                    scroll_steps=2)
            if parsing.is_signin_page(html, page.url):
                out["login_required"] = True
                break
            out["pages_checked"] += 1
            parsed = await asyncio.to_thread(parsing.parse_review_page, html)
            if out["total_reviews"] is None:
                out["total_reviews"] = parsed["total_reviews"]
            fresh = [r for r in parsed["reviews"]
                     if (r["review_id"] or id(r)) not in seen]
            seen.update(r["review_id"] or id(r) for r in fresh)
            out["reviews"] += fresh
            if not fresh or not parsed["has_next"]:
                break
        return out

    async def _scrape_product(self, ctx: BrowserContext, card: dict) -> dict | None:
        asin = card["asin"]
        url = f"{self.cfg.marketplace.base_url}/dp/{asin}"
        page = None
        try:
            page = await ctx.new_page()
            html = await self._goto(page, url, wait_for="#productTitle", scroll_steps=random.randint(7, 10))
            try:
                detail = await asyncio.to_thread(
                    parsing.parse_product_page, html, asin,
                    self.cfg.marketplace.base_url, self.cfg.marketplace.currency)
            except ValueError:
                self._save_debug(f"dp_{asin}", html)
                raise
            scraped_at = datetime.now(timezone.utc).isoformat()

            review_info = {"reviews": [], "pages_checked": 0, "total_reviews": None,
                           "login_required": False}
            has_reviews = bool(detail["reviews"]) or bool(detail["rating"]["total_ratings"])
            # Once Amazon has asked an unsigned session to sign in, don't keep
            # loading review pages for every product; just report it.
            login_known_required = self.review_login_required > 0 and not self.signed_in
            if login_known_required and has_reviews:
                self.review_login_required += 1
            elif self.cfg.review_pages > 0 and has_reviews:
                try:
                    review_info = await self._collect_review_pages(page, asin)
                except BrowserClosedError:
                    raise
                except Exception as exc:  # reviews are best-effort; keep the product
                    _log.warning("event=review_pages_failed asin=%s error=%s", asin, exc)
                    review_info["error"] = str(exc)
                if review_info["login_required"]:
                    self.review_login_required += 1

            reviews = parsing.merge_reviews(review_info["reviews"], detail["reviews"])
            if self.cfg.verified_reviews_only:
                reviews = [r for r in reviews if r["verified_purchase"]]
            detail["reviews"] = reviews

            _log.info("event=product_extracted asin=%s price=%s %s specs=%d images=%d reviews=%d title=%r",
                      asin, detail["pricing"]["currency"], detail["pricing"]["current_price"],
                      len(detail["specifications"]), len(detail["media"]["images"]),
                      len(reviews), detail["title"][:60])
            return {
                "product_details": {
                    "product_id": asin,
                    "product_name": detail["title"],
                    "product_detail_url": url,
                    "scraped_at": scraped_at,
                },
                "marketplace": self.cfg.marketplace.domain,
                "is_sponsored": card["is_sponsored"],
                "search_card": card,
                "detail": detail,
                "review_collection": {k: v for k, v in review_info.items() if k != "reviews"},
            }
        except Exception as exc:
            if isinstance(exc, (BrowserClosedError, BlockedError)) or _is_closed_error(exc):
                # Fatal for the whole run: stop cleanly and keep what we have.
                self.aborted = self.aborted or f"{exc.__class__.__name__}: {exc}"
                self._closed = self._closed or not isinstance(exc, BlockedError)
                _log.error("event=run_aborted asin=%s reason=%s", asin, self.aborted)
            else:
                _log.error("event=product_failed asin=%s error=%s", asin, exc)
            self.failures.append({"asin": asin, "error": str(exc)[:300]})
            return None
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    def _keep_card(self, card: dict, seen: set[str]) -> bool:
        if card["asin"] in seen:
            self.skipped["duplicate"] += 1
            return False
        seen.add(card["asin"])
        if card["is_sponsored"] and not self.cfg.include_sponsored:
            self.skipped["sponsored"] += 1
            return False
        title = (card.get("title") or "").lower()
        if any(k.lower() in title for k in self.cfg.exclude_keywords if k):
            self.skipped["excluded_keyword"] += 1
            return False
        return True

    # ── Orchestration ────────────────────────────────────────────────────

    async def run(self) -> list[dict]:
        cfg = self.cfg
        models: list[dict] = []
        page_limit = cfg.max_pages if cfg.max_pages > 0 else 50
        _log.info("event=engine_start marketplace=%s search=%r sort=%s max_pages=%s max_products=%s "
                  "review_pages=%s", cfg.marketplace.domain, cfg.search_text, cfg.sort_by,
                  cfg.max_pages, cfg.max_products, cfg.review_pages)

        async with async_playwright() as pw:
            try:
                ctx = await self._launch(pw)
            except Exception as exc:
                _log.error("event=browser_launch_failed error=%s", exc)
                raise
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await self._open_home(page)
                if cfg.review_pages > 0 and self.signed_in is False:
                    _log.warning("event=not_signed_in message=review pages need a signed-in Amazon "
                                 "session; sign in manually once in the scraper browser profile. "
                                 "Only product-page reviews will be collected.")

                seen: set[str] = set()
                sem = asyncio.Semaphore(max(1, cfg.detail_concurrency))
                url: str | None = build_search_url(cfg, 1)
                for page_num in range(1, page_limit + 1):
                    if url is None or len(models) >= cfg.max_products:
                        break
                    _log.info("event=page_start page=%d url=%s", page_num, url[:120])
                    try:
                        html = await self._goto(page, url, wait_for=_RESULT_SEL, scroll_steps=3)
                    except Exception as exc:
                        _log.error("event=search_page_failed page=%d error=%s", page_num, exc)
                        self.aborted = self.aborted or f"search page {page_num}: {exc}"
                        break
                    cards, next_url = parsing.parse_search_page(html, cfg.marketplace.base_url)
                    self.pages_processed += 1
                    if not cards:
                        self._save_debug(f"search_page{page_num}", html)
                        _log.warning("event=no_results page=%d stopping", page_num)
                        break
                    candidates = [c for c in cards if self._keep_card(c, seen)]
                    _log.info("event=search_parsed page=%d cards=%d candidates=%d",
                              page_num, len(cards), len(candidates))

                    async def _one(card):
                        async with sem:
                            if len(models) >= cfg.max_products or self.aborted:
                                return
                            m = await self._scrape_product(ctx, card)
                            if m and len(models) < cfg.max_products:
                                models.append(m)

                    await asyncio.gather(*[_one(c) for c in candidates])
                    _log.info("event=page_done page=%d total=%d", page_num, len(models))
                    if self.aborted:
                        _log.warning("event=run_stopped_early products=%d reason=%s",
                                     len(models), self.aborted)
                        break
                    url = next_url or build_search_url(cfg, page_num + 1)
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
                _log.info("event=browser_closed")

        _log.info("event=engine_done products=%d failures=%d", len(models), len(self.failures))
        return models
