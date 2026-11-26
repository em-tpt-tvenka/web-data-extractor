
from __future__ import annotations

import asyncio
import json
import math
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from app.core.logging import get_logger
from app.platforms.amazon import parsing
from app.platforms.browser import proxy_settings, random_user_agent
from app.platforms.amazon.options import Marketplace
from app.platforms.perf import RateLimiter, RunStats

_log = get_logger("amazon.engine")

_SORT_MAP = {
    "relevance": None,  # Amazon's default ("relevanceblender")
    "price_asc": "price-asc-rank",
    "price_desc": "price-desc-rank",
    "rating": "review-rank",
    "newest": "date-desc-rank",
}
_RESULT_SEL = "div[data-component-type='s-search-result']"
# Keep paging the search results until max_products is reached even when
# max_pages is small: the signed-in / ad-heavy layout drops many page-1 cards as
# sponsored, so one page often yields far fewer than max_products.
_SEARCH_BACKFILL_CAP = 15
_EXPANDER_SELS = (
    "#productDetails_expanderTables_depthLeftSections a.a-expander-header[aria-expanded='false']",
    "#productDetails_expanderTables_depthRightSections a.a-expander-header[aria-expanded='false']",
    "#prodDetails a.a-expander-header[aria-expanded='false']",
)


class BlockedError(Exception):
    """CAPTCHA could not be cleared (headless, or nobody solved it in time)."""


class ThrottledError(BlockedError):
    """Amazon kept answering with its 503 "rush hour" throttling page after
    every back-off. Fatal for the run (later pages would be refused too);
    partial results are kept."""


class RateLimitCooldown(Exception):
    """A previous run was rate-limited by Amazon less than ``cooldown_minutes``
    ago; this run stops before sending any request (sending more would only
    extend the block)."""


class ReviewPagesBlocked(Exception):
    """Amazon answers valid review-listing URLs with its 404 "not a functioning
    page" page even after back-off: review pages are refused to this session.
    Ends review collection for the product (product data is kept)."""


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
    review_sort: str = "recent"
    verified_reviews_only: bool = False
    review_click_min_delay: float = 0.8
    review_click_max_delay: float = 1.8
    keyword_stall_limit: int = 15
    # Keyword phase also ends when the last ``keyword_yield_window`` keyword
    # listings together found fewer than ``keyword_min_yield`` new reviews.
    keyword_yield_window: int = 8
    keyword_min_yield: int = 3
    # Amazon's 503 "rush hour" throttling: waits of 30, 60, 120 s (+ jitter).
    throttle_backoff_seconds: float = 30.0
    throttle_retries: int = 3
    max_requests_per_minute: float = 20.0  # all requests combined; 0 = no overall cap
    cooldown_minutes: float = 60.0

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


_STARS = (("five_star", 5), ("four_star", 4), ("three_star", 3), ("two_star", 2), ("one_star", 1))
# Amazon shows at most this many reviews per review listing (verified live:
# "Show 10 more reviews" disappears after 100 even when thousands exist).
LISTING_CAP = 100


def base_review_filters(cfg: AmazonEngineConfig) -> dict[str, str]:
    return {
        "reviewerType": "avp_only_reviews" if cfg.verified_reviews_only else "all_reviews",
        "sortBy": "helpful" if cfg.review_sort == "helpful" else "recent",
    }


def build_review_url(cfg: AmazonEngineConfig, asin: str, page: int = 1,
                     filters: dict[str, str] | None = None) -> str:
    """Review listing URL. Amazon ignores ``pageNumber`` (further reviews load
    in place via "Show 10 more reviews"); it is kept so the URL stays stable.
    ``filters`` narrows the listing (filterByStar, sortBy, mediaType,
    formatType, reviewerType, filterByKeyword)."""
    params = {**base_review_filters(cfg), **(filters or {}), "pageNumber": str(page)}
    return f"{cfg.marketplace.base_url}/product-reviews/{asin}/?{urllib.parse.urlencode(params)}"


class AmazonEngine:
    def __init__(self, cfg: AmazonEngineConfig):
        self.cfg = cfg
        self.pages_processed = 0
        self.signed_in: bool | None = None
        self.failures: list[dict] = []
        self.skipped: dict[str, int] = {"sponsored": 0, "excluded_keyword": 0, "duplicate": 0}
        self.review_login_required = 0
        self.reviews_incomplete = 0  # products whose extracted reviews < the reported count
        self.aborted: str | None = None  # why the run stopped early, if it did
        self._closed = False
        # One request budget per run, shared by every concurrent product worker:
        # page loads and "Show 10 more reviews" requests each keep their own
        # randomized minimum gap, so concurrency never raises the request rate
        # Amazon sees (live runs showed a CAPTCHA wall after sustained traffic).
        self._nav = RateLimiter(cfg.min_delay_seconds, cfg.max_delay_seconds)
        self._click = RateLimiter(cfg.review_click_min_delay, cfg.review_click_max_delay)
        # Overall budget shared by page loads and "show more" (max_requests_per_minute).
        gap = 60.0 / cfg.max_requests_per_minute if cfg.max_requests_per_minute > 0 else 0.0
        self._budget = RateLimiter(gap, gap * 1.3)
        self.stats = RunStats()
        self.throttle_events = 0  # Amazon 503 "rush hour" responses seen this run
        self._last_status: dict[int, int | None] = {}  # id(page) -> HTTP status of its last load
        # Review pages refused with Amazon's 404 page (soft block of this session).
        self.review_block_events = 0
        self.review_pages_blocked = False  # confirmed: later products probe once, no back-off
        self.products_reviews_blocked = 0
        # Optional orchestrator hooks (incremental storage / resume); see app.storage.incremental.
        self.hooks = None
        self.emit_filter = None  # adapter post-filter: products it would drop are not stored

    @property
    def requests_sent(self) -> int:
        return self._nav.requests + self._click.requests

    # ── cool-down between runs after Amazon rate-limits this session ─────

    def _cooldown_file(self) -> Path | None:
        return Path(self.cfg.profile_dir) / ".amazon_cooldown.json" if self.cfg.profile_dir else None

    def cooldown_remaining(self) -> tuple[datetime, datetime] | None:
        """``(limited_at, allowed_after)`` while a recorded rate limit is still
        cooling down, else None."""
        f = self._cooldown_file()
        if f is None or self.cfg.cooldown_minutes <= 0 or not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            limited_at = datetime.fromisoformat(data["limited_at"])
        except Exception:
            return None
        allowed = limited_at + timedelta(minutes=self.cfg.cooldown_minutes)
        return (limited_at, allowed) if datetime.now(timezone.utc) < allowed else None

    def _record_rate_limit(self, reason: str) -> None:
        f = self._cooldown_file()
        if f is None:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"limited_at": datetime.now(timezone.utc).isoformat(), "reason": reason[:300]}),
                         encoding="utf-8")
        except Exception as exc:
            _log.warning("event=cooldown_record_failed error=%s", exc)

    # ── Browser helpers ──────────────────────────────────────────────────

    async def _launch(self, pw) -> BrowserContext:
        cfg, mp = self.cfg, self.cfg.marketplace
        opts = dict(
            headless=cfg.headless, locale=mp.locale, timezone_id=mp.timezone,
            viewport={"width": 1440, "height": 900},
            user_agent=random_user_agent(),
            proxy=proxy_settings(),
            extra_http_headers={"Accept-Language": mp.accept_language},
            args=["--disable-blink-features=AutomationControlled"],
        )
        _log.info("event=browser_launching channel=%s headless=%s profile=%s",
                  cfg.browser_channel, cfg.headless, cfg.profile_dir)
        # Keep Chrome's sandbox on: Playwright otherwise passes --no-sandbox,
        # which shows "unsupported command-line flag" and weakens isolation.
        if cfg.profile_dir:
            Path(cfg.profile_dir).mkdir(parents=True, exist_ok=True)
            ctx = await pw.chromium.launch_persistent_context(
                cfg.profile_dir, channel=cfg.browser_channel, chromium_sandbox=True, **opts)
        else:
            browser = await pw.chromium.launch(
                headless=cfg.headless, channel=cfg.browser_channel, chromium_sandbox=True,
                args=opts.pop("args"))
            ctx = await browser.new_context(**{k: v for k, v in opts.items() if k != "headless"})
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        ctx.on("close", lambda *_: setattr(self, "_closed", True))
        return ctx

    async def _throttle(self) -> None:
        """Polite, randomized gap between navigations, shared across workers,
        within the overall requests-per-minute budget."""
        await self._nav.acquire()
        await self._budget.acquire()

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
        """Load a page with retries and backoff; returns its HTML. Amazon's 503
        "rush hour" throttling page is retried after progressively longer
        pauses (see ``_back_off_throttled``) instead of being parsed as an
        empty result; it does not use up the retries for network errors."""
        last_err: Exception | None = None
        attempt, throttled = 0, 0
        while attempt < self.cfg.max_retries:
            if self._closed or page.is_closed():
                raise BrowserClosedError("browser was closed")
            await self._throttle()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                status = resp.status if resp is not None else None
                self._last_status[id(page)] = status
                if status in (429, 503) or parsing.looks_throttled(await page.content()):
                    throttled += 1
                    await self._back_off_throttled(url, throttled, status)
                    continue
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
                if parsing.looks_throttled(html):
                    throttled += 1
                    await self._back_off_throttled(url, throttled, status)
                    continue
                return html
            except (BlockedError, BrowserClosedError):
                raise
            except Exception as exc:
                if _is_closed_error(exc) or page.is_closed():
                    raise BrowserClosedError("browser was closed") from exc
                attempt += 1
                last_err = exc
                backoff = 5 * attempt + random.uniform(0, 3)
                _log.warning("event=load_failed attempt=%d/%d retry_in=%.0fs url=%s error=%s",
                             attempt, self.cfg.max_retries, backoff, url[:100], exc.__class__.__name__)
                await asyncio.sleep(backoff)
        raise PageLoadError(f"giving up on {url}: {last_err}")

    async def _back_off_throttled(self, url: str, n: int, status: int | None = None) -> None:
        """Amazon answered with its throttling page (HTTP 503 "It's rush hour and
        traffic is piling up ... try again in a short while"). Wait
        ``throttle_backoff_seconds`` x 2^(n-1) (+ jitter) and slow the shared
        request budget for the rest of the run; after ``throttle_retries``
        consecutive refusals stop the run with a clear reason."""
        cfg = self.cfg
        self.throttle_events += 1
        if n > cfg.throttle_retries:
            msg = (f"Amazon kept returning its HTTP 503 'rush hour' throttling page ({n - 1} retries with "
                   f"back-off); this session/IP is rate-limited - wait before running again")
            self._record_rate_limit(msg)
            raise ThrottledError(msg)
        base = cfg.throttle_backoff_seconds * 2 ** (n - 1)
        wait = base + random.uniform(0, base / 3)
        self._nav.slow_down(1.5, cap=4.0)
        self._click.slow_down(1.5, cap=4.0)
        _log.warning("event=amazon_throttled status=%s attempt=%d/%d wait=%.0fs page_gap=%.1f-%.1fs url=%s",
                     status, n, cfg.throttle_retries, wait, self._nav.min_interval, self._nav.max_interval,
                     url[:100])
        await asyncio.sleep(wait)

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

    async def _show_more(self, page: Page, asin: str, label: str) -> tuple[int, str]:
        """Load the rest of the open review listing with "Show 10 more reviews"
        (Amazon appends each batch in place; URL page numbers are ignored).
        Returns ``(batches_loaded, stop_reason)``. Each click is retried; a
        CAPTCHA is handled as on page loads. Always loads every review the
        listing offers, stopping only when the button is gone (the listing is
        exhausted) -- never on a review count."""
        cfg = self.cfg
        review_count_js = "n => document.querySelectorAll(\"[data-hook='review']\").length > n"
        batches, throttled = 1, 0
        while True:
            btn = page.locator(parsing.SHOW_MORE_SEL).first
            if await btn.count() == 0 or not await btn.is_visible():
                return batches, "listing exhausted"
            before = await page.locator("[data-hook='review']").count()
            for attempt in range(1, cfg.max_retries + 1):
                await self._click.acquire()  # shared request budget across workers
                await self._budget.acquire()  # overall requests-per-minute cap
                try:
                    await btn.scroll_into_view_if_needed(timeout=5_000)
                    async with page.expect_response(lambda r: "/ajax/reviews/get" in r.url,
                                                    timeout=20_000) as resp_info:
                        await btn.click(timeout=10_000)
                    status = (await resp_info.value).status
                    if status in (429, 503):  # throttled: back off, then click again
                        throttled += 1
                        await self._back_off_throttled(page.url, throttled, status)
                        continue
                    await page.wait_for_function(review_count_js, arg=before, timeout=20_000)
                    break
                except BlockedError:
                    raise
                except Exception as exc:
                    if _is_closed_error(exc) or page.is_closed() or self._closed:
                        raise BrowserClosedError("browser was closed") from exc
                    await self._resolve_captcha(page)  # raises BlockedError if it can't be cleared
                    _log.warning("event=review_batch_retry asin=%s listing=%s batch=%d attempt=%d/%d error=%s",
                                 asin, label, batches + 1, attempt, cfg.max_retries, exc.__class__.__name__)
                    btn = page.locator(parsing.SHOW_MORE_SEL).first
                    if await btn.count() == 0:
                        return batches, "listing exhausted"
                    await asyncio.sleep(3 * attempt)
            else:
                return batches, f"show-more failed after {cfg.max_retries} attempts (batch {batches + 1})"
            batches += 1
            _log.debug("event=review_batch_loaded asin=%s listing=%s batch=%d", asin, label, batches)

    async def _load_listing(self, page: Page, asin: str, filters: dict[str, str], label: str,
                            have=None) -> dict:
        """One review listing (one filter combination), loaded to its end.
        ``have`` (optional callable) returns how many of this listing's reviews
        are already collected; when the listing reports no more than that, its
        further pages cannot hold anything new and are not requested."""
        out = {"reviews": [], "total": None, "batches": 0, "stop_reason": None,
               "login_required": False, "error": None}
        url = build_review_url(self.cfg, asin, 1, filters)
        refused = 0
        while True:
            html = await self._goto(page, url,
                                    wait_for="[data-hook='review'], [data-hook='cr-filter-info-review-rating-count'], "
                                             "#ap_email, #cm_cr-review_list",
                                    scroll_steps=1)
            if not parsing.looks_not_found(html, page.url, self._last_status.get(id(page))):
                self.review_pages_blocked = False  # Amazon serves review pages (again)
                break
            # Valid review URL answered with "not a functioning page": Amazon
            # refusing review pages to this session. Back off and retry; once
            # confirmed, later products probe once without waiting.
            refused += 1
            self.review_block_events += 1
            if self.review_pages_blocked or refused > self.cfg.throttle_retries:
                msg = (f"Amazon answered review pages with 'Page Not Found' (HTTP "
                       f"{self._last_status.get(id(page))}) {refused} time(s); review pages are refused to this "
                       "session (rate-limited) - wait before running again")
                if not self.review_pages_blocked:
                    self._record_rate_limit(msg)
                self.review_pages_blocked = True
                raise ReviewPagesBlocked(msg)
            base = self.cfg.throttle_backoff_seconds * 2 ** (refused - 1)
            wait = base + random.uniform(0, base / 3)
            self._nav.slow_down(1.5, cap=4.0)
            self._click.slow_down(1.5, cap=4.0)
            _log.warning("event=review_page_refused asin=%s listing=%s status=%s attempt=%d/%d wait=%.0fs",
                         asin, label, self._last_status.get(id(page)), refused, self.cfg.throttle_retries, wait)
            await asyncio.sleep(wait)
        if parsing.is_signin_page(html, page.url):
            out.update(login_required=True, stop_reason="sign-in required")
            return out
        try:
            first = parsing.parse_review_page(html)
            known = have() if have else None
            if first["total_reviews"] is not None and known is not None and first["total_reviews"] <= known:
                out["batches"], out["stop_reason"] = 1, "already collected"
            elif 'data-hook="review"' in html or "data-hook='review'" in html:
                out["batches"], out["stop_reason"] = await self._show_more(page, asin, label)
            else:
                out["stop_reason"] = "no reviews in listing"
            html = await page.content()
        except (BrowserClosedError, BlockedError):
            raise
        except Exception as exc:  # keep what this listing loaded before the failure
            out["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            out["stop_reason"] = "error"
            try:
                html = await page.content()
            except Exception:
                pass
        parsed = await asyncio.to_thread(parsing.parse_review_page, html)
        out["reviews"], out["total"] = parsed["reviews"], parsed["total_reviews"]
        return out

    async def _collect_review_pages(self, page: Page, asin: str) -> dict:
        """Every review Amazon makes reachable for ``asin``.

        Amazon shows at most ``LISTING_CAP`` (100) reviews per review listing,
        but every filtered listing has its own window. So after the base listing
        the reviews are partitioned by star rating; a star group larger than one
        window is split further (other sort order, media-only, current format,
        verified-only) and then by keyword, using words mined from the reviews
        already collected, until the group's reported count is reached or
        ``keyword_stall_limit`` keyword listings in a row add nothing new.
        Reviews are de-duplicated by review id across all listings. There is no
        review-count limit; completeness is measured and reported."""
        cfg = self.cfg
        out = {"reviews": [], "pages_checked": 0, "total_reviews": None, "login_required": False,
               "stop_reason": None, "listings": [], "failed_listings": [], "star_coverage": {}}
        found: dict = {}
        tried: set = set()
        base = base_review_filters(cfg)

        class _LoginRequired(Exception):
            pass

        async def window(extra: dict[str, str], label: str, have=None) -> dict | None:
            filters = {**base, **extra}
            key = tuple(sorted(filters.items()))
            if key in tried:
                return None
            tried.add(key)
            try:
                res = await self._load_listing(page, asin, filters, label, have)
            except (BrowserClosedError, BlockedError):
                raise
            except ReviewPagesBlocked as exc:
                out["failed_listings"].append({"listing": label, "error": f"ReviewPagesBlocked: {exc}"})
                raise
            except Exception as exc:  # page failed after retries: record it, try the next listing
                err = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                out["failed_listings"].append({"listing": label, "error": err})
                _log.warning("event=review_listing_failed asin=%s listing=%s error=%s", asin, label, err)
                return None
            if res["login_required"]:
                out["login_required"] = True
                raise _LoginRequired(label)
            new = 0
            for r in res["reviews"]:
                k = parsing.review_key(r)
                if k in found:
                    found[k] = parsing.merge_reviews([found[k]], [r])[0]
                else:
                    found[k] = r
                    new += 1
            res["new"] = new
            out["pages_checked"] += res["batches"]
            entry = {"listing": label, "available": res["total"], "loaded": len(res["reviews"]),
                     "new": new, "pages": res["batches"], "stop_reason": res["stop_reason"]}
            if res["error"]:
                entry["error"] = res["error"]
                out["failed_listings"].append({"listing": label, "error": res["error"]})
            out["listings"].append(entry)
            _log.info("event=review_listing_done asin=%s listing=%s available=%s loaded=%d new=%d "
                      "unique_total=%d pages=%d stop=%s", asin, label, res["total"], len(res["reviews"]),
                      new, len(found), res["batches"], res["stop_reason"])
            return res

        def capped(res: dict | None) -> bool:
            """The listing may hold more than it showed (window full or cut short)."""
            return bool(res) and (len(res["reviews"]) >= LISTING_CAP or res["error"] is not None)

        def star_count(value: int) -> int:
            return sum(1 for r in found.values() if r.get("rating") == value)

        def keyword_count(value: int, kw: str) -> int:
            rx = re.compile(rf"\b{re.escape(kw)}\b", re.I)
            return sum(1 for r in found.values() if r.get("rating") == value
                       and rx.search(f"{r.get('title') or ''} {r.get('review_text') or ''}"))

        try:
            first = await window({}, "all")
            total = first["total"] if first else None
            out["total_reviews"] = total
            _log.info("event=review_total_found asin=%s available=%s", asin, total)
            if (total is not None and len(found) < total) or \
                    (total is None and (first is None or capped(first))):
                # Partition whenever the base listing did not deliver everything it
                # reports - full at 100, or cut short below it (seen live: 53 of 118).
                for star, value in _STARS:
                    s = await window({"filterByStar": star, "sortBy": "recent"}, f"{star}/recent",
                                     have=lambda value=value: star_count(value))
                    star_total = s["total"] if s else None

                    def complete() -> bool:
                        return star_total is not None and star_count(value) >= star_total

                    if s is not None and s["stop_reason"] == "already collected":
                        pass  # the base listing already held this whole star group
                    elif s is None or capped(s):
                        extras = [({"filterByStar": star, "sortBy": "helpful"}, f"{star}/helpful"),
                                  ({"filterByStar": star, "mediaType": "media_reviews_only"}, f"{star}/media"),
                                  ({"filterByStar": star, "formatType": "current_format"}, f"{star}/current_format")]
                        if not cfg.verified_reviews_only:
                            extras.append(({"filterByStar": star, "reviewerType": "avp_only_reviews"},
                                           f"{star}/verified"))
                        for extra, label in extras:
                            if complete():
                                break
                            await window(extra, label)
                        used: set[str] = set()
                        stall = 0
                        recent: list[int] = []  # new reviews per keyword listing, most recent last
                        while not complete() and stall < cfg.keyword_stall_limit:
                            if len(recent) >= cfg.keyword_yield_window > 0 and \
                                    sum(recent[-cfg.keyword_yield_window:]) < cfg.keyword_min_yield:
                                _log.info("event=keyword_phase_low_yield asin=%s star=%s last=%d new=%d",
                                          asin, star, cfg.keyword_yield_window,
                                          sum(recent[-cfg.keyword_yield_window:]))
                                break
                            words = parsing.review_keywords(
                                [r for r in found.values() if r.get("rating") == value], used, limit=1)
                            if not words:
                                break
                            kw = words[0]
                            used.add(kw)
                            w = await window({"filterByStar": star, "sortBy": "recent", "filterByKeyword": kw},
                                             f"{star}/kw:{kw}",
                                             have=lambda value=value, kw=kw: keyword_count(value, kw))
                            gained = (w or {}).get("new", 0)
                            if capped(w):  # keyword listing also full: its other sort order too
                                w2 = await window({"filterByStar": star, "sortBy": "helpful",
                                                   "filterByKeyword": kw}, f"{star}/kw:{kw}/helpful")
                                gained += (w2 or {}).get("new", 0)
                            recent.append(gained)
                            stall = 0 if gained else stall + 1
                    out["star_coverage"][star] = {"available": star_total, "extracted": star_count(value)}
                out["stop_reason"] = "partitioned listings exhausted"
            else:
                out["stop_reason"] = (first or {}).get("stop_reason") or "listing failed"
        except _LoginRequired as exc:
            out["stop_reason"] = f"sign-in required ({exc})"
        except ReviewPagesBlocked as exc:
            out["review_pages_blocked"] = True
            out["stop_reason"] = f"review pages refused by Amazon: {exc}"
            self.products_reviews_blocked += 1
            _log.error("event=review_pages_blocked asin=%s collected=%d reason=%s", asin, len(found), exc)

        out["reviews"] = list(found.values())
        _log.info("event=review_collection_done asin=%s available=%s extracted=%d listings=%d pages=%d "
                  "failed_listings=%d", asin, out["total_reviews"], len(found), len(out["listings"]),
                  out["pages_checked"], len(out["failed_listings"]))
        return out

    async def _scrape_reviews(self, page: Page, data: dict) -> tuple[dict, list[dict]]:
        """Flipkart-style review step: returns ``(review_summary, reviews)``.
        Product-page reviews are merged with review-page reviews; review pages
        need a signed-in session, so without one only product-page reviews remain."""
        cfg, asin = self.cfg, data["product_id"]
        ratings = data["ratings"]
        info = {"reviews": [], "pages_checked": 0, "total_reviews": None,
                "login_required": False, "stop_reason": "review pages not requested"}
        has_reviews = bool(data["reviews"]) or bool(ratings["total_ratings"])
        # Once Amazon has asked an unsigned session to sign in, don't keep
        # loading review pages for every product; just report it.
        login_known_required = self.review_login_required > 0 and not self.signed_in
        if not has_reviews:
            info["stop_reason"] = "product has no ratings"
        elif login_known_required:
            self.review_login_required += 1
            info.update(login_required=True, stop_reason="sign-in required (known from earlier product)")
        else:
            try:
                info = await self._collect_review_pages(page, asin)
            except BrowserClosedError:
                raise
            except Exception as exc:  # reviews are best-effort; keep the product
                _log.warning("event=review_pages_failed asin=%s error=%s", asin, exc)
                info["stop_reason"] = f"error: {exc.__class__.__name__}"
                info["error"] = str(exc)[:300]
            if info["login_required"]:
                self.review_login_required += 1

        reviews = parsing.merge_reviews(info["reviews"], data["reviews"])
        if cfg.verified_reviews_only:
            reviews = [r for r in reviews if r["verified_purchase"]]
        for r in reviews:
            r["asin"] = asin
            r["marketplace"] = cfg.marketplace.domain

        if info["total_reviews"] is not None:
            ratings["total_reviews"] = info["total_reviews"]
        coverage = self._coverage(info, len(reviews))
        if coverage["complete"] is False:
            self.reviews_incomplete += 1
            _log.warning("event=reviews_incomplete asin=%s available=%s extracted=%d coverage=%s%% reason=%s",
                         asin, coverage["reviews_available"], len(reviews), coverage["coverage_percent"],
                         coverage["incomplete_reason"])
        _log.info("event=reviews_done asin=%s available=%s extracted=%d pages=%d listings=%d",
                  asin, coverage["reviews_available"], len(reviews), info["pages_checked"],
                  len(info.get("listings") or []))
        summary = {
            "total_ratings": ratings["total_ratings"],
            "total_reviews": ratings["total_reviews"],
            "average_rating": ratings["average_rating"],
            "rating_breakdown": ratings["breakdown"],
            "rating_breakdown_percent": ratings["breakdown_percent"],
            "positive_feedback_percent": None,
            "negative_feedback_percent": None,
            "aspect_ratings": data.get("aspect_ratings") or {},
            "customers_say": data.get("customers_say"),
            "review_url": build_review_url(cfg, asin, 1),
            "pages_scraped": info["pages_checked"],
            "stop_reason": info["stop_reason"],
            "sort_used": cfg.review_sort,
            "login_required": info["login_required"],
            # Completeness: what the review listing reports vs. what was extracted.
            **coverage,
            "collection_mode": "all",
            "listing_cap": LISTING_CAP,
            "listings_scraped": len(info.get("listings") or []),
            "star_coverage": info.get("star_coverage") or {},
            "failed_listings": info.get("failed_listings") or [],
            "review_pages_blocked": bool(info.get("review_pages_blocked")),
            "listings": info.get("listings") or [],
        }
        if info.get("error"):
            summary["error"] = info["error"]
        return summary, reviews

    def _coverage(self, info: dict, extracted: int) -> dict:
        """Reported review count vs. unique reviews extracted. ``complete`` is
        None when Amazon reported no count or review pages were not loaded."""
        total = info.get("total_reviews")
        out = {"reviews_available": total, "reviews_extracted": extracted,
               "coverage_percent": round(min(extracted, total) / total * 100, 1) if total else None,
               "complete": None, "incomplete_reason": None}
        if total is None:
            return out
        out["complete"] = extracted >= total
        if out["complete"]:
            return out
        if info.get("review_pages_blocked"):
            reason = ("Amazon stopped serving review pages to this session ('Page Not Found' on valid "
                      "review URLs = rate limit); reviews collected before that are kept")
        elif info.get("login_required"):
            reason = "review pages need a signed-in Amazon session"
        elif info.get("failed_listings"):
            reason = f"{len(info['failed_listings'])} review listing(s) failed to load"
        else:
            reason = (f"Amazon shows at most {LISTING_CAP} reviews per listing; the remaining reviews "
                      "were not reachable through any star / sort / media / format / keyword listing")
        out["incomplete_reason"] = reason
        return out

    async def _scrape_product(self, ctx: BrowserContext, card: dict) -> dict | None:
        asin = card["asin"]
        url = f"{self.cfg.marketplace.base_url}/dp/{asin}"
        page = None
        ps = self.stats.product(asin)
        requests_before = self.requests_sent
        try:
            page = await ctx.new_page()
            html = await self._goto(page, url, wait_for="#productTitle", scroll_steps=random.randint(7, 10))
            try:
                parsed = await asyncio.to_thread(
                    parsing.parse_product_page, html, asin,
                    self.cfg.marketplace.base_url, self.cfg.marketplace.currency)
            except ValueError:
                self._save_debug(f"dp_{asin}", html)
                raise

            # Flat product model, same top-level shape as Flipkart's engine output.
            data: dict = {
                "url": url,
                "product_id": asin,
                "is_sponsored": bool(card["is_sponsored"]),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "marketplace": self.cfg.marketplace.domain,
                **parsed,
                "search_card": card,
            }
            # Kept so the orchestrator's product de-duplication (keyed on
            # product_details.product_id) still applies to Amazon.
            data["product_details"] = {
                "product_id": asin, "product_name": data["product_name"],
                "product_detail_url": url, "scraped_at": data["scraped_at"],
            }
            data["review_summary"], data["reviews"] = await self._scrape_reviews(page, data)

            s = data["review_summary"]
            ps.reviews_available = s.get("reviews_available")
            ps.pages_total = math.ceil(ps.reviews_available / 10) if ps.reviews_available else None
            ps.pages_processed = s.get("pages_scraped") or 0
            ps.listings_processed = s.get("listings_scraped") or 0
            ps.reviews_extracted = len(data["reviews"])
            ps.failed_pages = len(s.get("failed_listings") or [])
            ps.requests = self.requests_sent - requests_before  # approximate when products overlap
            ps.finish("done")
            data["scrape_stats"] = ps.as_dict()

            _log.info("event=product_extracted asin=%s price=%s %s specs=%d images=%d videos=%d "
                      "reviews=%d available=%s seconds=%s title=%r", asin, data["price"]["currency"],
                      data["price"]["selling_price"], len(data["specifications_flat"]), len(data["images"]),
                      len(data["videos"]), len(data["reviews"]), ps.reviews_available, ps.seconds,
                      data["product_name"][:60])
            return data
        except Exception as exc:
            ps.finish("failed")
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

    # ── Incremental storage / resume hooks ───────────────────────────────

    async def _already_completed(self, asin: str) -> bool:
        """Resume: a product an earlier run finished (recently) is not scraped
        again; its data is already stored."""
        if self.hooks is None:
            return False
        try:
            done = await self.hooks.product_completed("amazon", asin)
        except Exception as exc:
            _log.warning("event=resume_check_failed asin=%s error=%s", asin, exc)
            return False
        if done:
            self.stats.product(asin).finish("skipped_completed")
            _log.info("event=product_skipped_completed asin=%s", asin)
        return bool(done)

    async def _emit(self, model: dict) -> None:
        """Hand a finished product to incremental storage; never fails the run."""
        if self.hooks is None:
            return
        try:
            if self.emit_filter is not None and not self.emit_filter(model):
                return
            await self.hooks.product_scraped("amazon", model)
        except Exception as exc:
            _log.warning("event=incremental_store_failed asin=%s error=%s", model.get("product_id"), exc)

    # ── Orchestration ────────────────────────────────────────────────────

    async def run(self) -> list[dict]:
        cfg = self.cfg
        models: list[dict] = []
        page_limit = cfg.max_pages if cfg.max_pages > 0 else 50
        # Backfill extra pages (bounded) to reach max_products; the loop still
        # stops as soon as len(models) >= max_products.
        page_limit = max(page_limit, _SEARCH_BACKFILL_CAP)
        cooling = self.cooldown_remaining()
        if cooling:
            limited_at, allowed = cooling
            raise RateLimitCooldown(
                f"Amazon rate-limited this session at {limited_at:%Y-%m-%d %H:%M} UTC; to avoid extending the "
                f"block, the next run is allowed after {allowed:%Y-%m-%d %H:%M} UTC "
                f"(amazon_cooldown_minutes={cfg.cooldown_minutes:g}; set 0 to disable)")
        _log.info("event=engine_start marketplace=%s search=%r sort=%s max_pages=%s max_products=%s",
                  cfg.marketplace.domain, cfg.search_text, cfg.sort_by, cfg.max_pages, cfg.max_products)

        async with async_playwright() as pw:
            try:
                ctx = await self._launch(pw)
            except Exception as exc:
                _log.error("event=browser_launch_failed error=%s", exc)
                raise
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await self._open_home(page)
                if self.signed_in is False:
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
                            if await self._already_completed(card["asin"]):
                                return
                            m = await self._scrape_product(ctx, card)
                            if m and len(models) < cfg.max_products:
                                models.append(m)
                                await self._emit(m)

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

        run = self.stats.summary(self.requests_sent)
        _log.info("event=engine_done products=%d failures=%d reviews_available=%d reviews_scraped=%d "
                  "requests=%d seconds=%s reviews_per_minute=%s avg_seconds_per_product=%s",
                  len(models), len(self.failures), run["reviews_available"], run["reviews_scraped"],
                  self.requests_sent, run["execution_seconds"], run["reviews_per_minute"],
                  run["avg_seconds_per_product"])
        return models
