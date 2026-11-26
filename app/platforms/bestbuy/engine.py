from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.async_api import Page, async_playwright

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms import net_routing
from app.platforms.bestbuy import parsing
from app.platforms.browser import random_user_agent

_log = get_logger("bestbuy.engine")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""
_BLOCK_RE = re.compile(r"access denied|are you a human|verify you are|captcha|unusual traffic", re.I)

_JS_LINKS = r"""
(maxProducts) => {
  const anchors = Array.from(document.querySelectorAll(
    'a[href*=".p"], a[href*="/product/"], a[href*="/site/"], a[href*="skuId="]'
  ));
  const seen = new Set(); const out = [];
  // Mirrors app/platforms/bestbuy/parsing.py's product_id_from_url: legacy
  // numeric SKUs before ".p", current alphanumeric SKUs as the last
  // /product/.../ segment, or a skuId= query param, in that order.
  const extractSku = (href) => {
    let m = href.match(/(\d{5,})\.p(?:[/?#]|$)/);
    if (m) return m[1];
    m = href.match(/\/product\/[^?#]*\/([A-Za-z0-9]{6,12})(?:[/?#]|$)/i);
    if (m && /\d/.test(m[1])) return m[1].toUpperCase();
    m = href.match(/skuId=([A-Za-z0-9]{5,12})/i);
    if (m) return m[1].toUpperCase();
    // Last resort: a bare 5+ digit run, but only as a whole token (word
    // boundary both sides) so digits embedded in category/campaign ids like
    // "abcat0010000.c" or "pcmcat1774297472214.c" aren't mistaken for SKUs.
    m = href.match(/\b(\d{5,})\b/);
    return m ? m[1] : null;
  };
  for (const a of anchors) {
    const href = a.href || "";
    const sku = extractSku(href);
    if (sku && !seen.has(sku)) { seen.add(sku); out.push(href); if (out.length >= maxProducts) break; }
  }
  return out;
}
"""
_JS_DOM = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const titleEl = document.querySelector('h1.heading-5, h1[class*="heading"], h1');
  const priceEl = document.querySelector('.priceView-hero-price span[aria-hidden="true"], [class*="priceView-customer-price"]');
  const regEl = document.querySelector('[class*="pricing-price__regular-price"], .pricing-price__regular-price, [aria-label*="regular price" i]');
  const brandEl = document.querySelector('a[href*="/site/brands/"], [class*="brand"] a');
  const descEl = document.querySelector('[class*="product-description"], [data-testid*="description"], #product-description');
  const availEl = document.querySelector('[class*="fulfillment-add-to-cart-button"] button, [data-button-state]');
  // bbystatic.com's image server (Scene7-style) encodes the requested size as
  // ";maxHeight=N;maxWidth=N" path segments BEFORE any "?" query string, so
  // splitting on "?" alone (the previous approach) does not normalize size
  // variants: the same image (identified by its UUID filename) was observed
  // live appearing at several different maxWidth values on one page and
  // being kept as separate "duplicate" images. Dedupe by the URL with the
  // size segment and query string stripped, keeping whichever observed
  // variant has the larger maxWidth (never inventing an un-observed size).
  const seenImg = new Map();
  document.querySelectorAll('img[src*="bbystatic.com"], img[src*="bestbuy.com"]').forEach(img => {
    let src = img.src || img.currentSrc || img.getAttribute('data-src');
    if (src && !src.includes('icon') && !src.includes('logo') && !src.includes('svg')) {
      const noQuery = src.split('?')[0];
      const m = noQuery.match(/;maxHeight=(\d+);maxWidth=(\d+)/i);
      const key = noQuery.replace(/;maxHeight=\d+;maxWidth=\d+/i, '');
      const width = m ? parseInt(m[2], 10) : 0;
      const cur = seenImg.get(key);
      if (!cur || width > cur.width) seenImg.set(key, {url: src, width});
    }
  });
  const images = Array.from(seenImg.values()).map(v => v.url);
  const videos = [];
  document.querySelectorAll('video source, video').forEach(v => {
    const s = v.src || v.currentSrc; if (s && s.startsWith('http') && !videos.includes(s)) videos.push(s);
  });
  // Rating fallback: Best Buy's shared rating-widget component (used both on
  // product tiles and, confirmed by the same "c-ratings-reviews"/"c-reviews"
  // class names and ReviewStatsContextualized component on the product
  // detail page's own review-summary block) renders a visually-hidden string
  // "Rating 4.7 out of 5 stars with 35 reviews" (or "Not Yet Reviewed (0
  // reviews)" when there are none) - the single most reliable source since
  // it is plain text rather than a computed star-width style.
  let ratingValue = null, reviewCount = null;
  const hiddenRating = Array.from(document.querySelectorAll('.visually-hidden, [class*="visually-hidden"]'))
    .map(e => clean(e.innerText)).find(t => /rating[^.]*out of 5 stars/i.test(t) || /not yet reviewed/i.test(t));
  if (hiddenRating) {
    const rm = hiddenRating.match(/([\d.]+)\s*out of 5 stars(?:\s*with\s*([\d,]+)\s*reviews?)?/i);
    if (rm) {
      ratingValue = parseFloat(rm[1]);
      if (rm[2]) reviewCount = parseInt(rm[2].replace(/,/g, ''), 10);
    } else if (/not yet reviewed/i.test(hiddenRating)) {
      reviewCount = 0;
    }
  }
  if (reviewCount === null) {
    const reviewsEl = document.querySelector('.c-reviews, [class*="c-reviews"]');
    if (reviewsEl) {
      const cm = clean(reviewsEl.innerText).match(/([\d,]+)/);
      if (cm) reviewCount = parseInt(cm[1].replace(/,/g, ''), 10);
    }
  }
  return {
    title: clean(titleEl && titleEl.innerText),
    priceText: clean(priceEl && priceEl.innerText),
    regularPriceText: clean(regEl && regEl.innerText),
    brand: clean(brandEl && brandEl.innerText),
    description: clean(descEl && descEl.innerText),
    availabilityText: clean(availEl && availEl.innerText),
    ratingValue, reviewCount,
    images, videos
  };
}
"""

_JS_SPECS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = {};
  const add = (k, v) => { k = clean(k); v = clean(v); if (k && v && k.length <= 80 && v.length <= 300 && !(k in out)) out[k] = v; };
  document.querySelectorAll(
    '.spec-row, [class*="spec-row"], li.spec, ul.specs li, .specifications-list li, [data-testid*="spec"] li'
  ).forEach(row => {
    const k = (row.querySelector('.row-title, [class*="title"], dt, .spec-label, th'));
    const v = (row.querySelector('.row-value, [class*="value"], dd, .spec-value, td'));
    if (k && v) add(k.innerText, v.innerText);
  });
  document.querySelectorAll('table tr').forEach(tr => {
    const cells = [...tr.querySelectorAll('th,td')].map(c => clean(c.innerText)).filter(Boolean);
    if (cells.length >= 2) add(cells[0], cells.slice(1).join(' '));
  });
  document.querySelectorAll('dl').forEach(dl => {
    const dts = dl.querySelectorAll('dt'), dds = dl.querySelectorAll('dd');
    for (let i = 0; i < Math.min(dts.length, dds.length); i++) add(dts[i].innerText, dds[i].innerText);
  });
  return out;
}
"""

_JS_REVIEWS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const nodes = document.querySelectorAll(
    'li.review-item, [class*="review-item"], .ugc-review, [data-testid*="review"] li, [itemprop="review"]'
  );
  nodes.forEach(n => {
    const whole = clean(n.innerText);
    if (whole.length < 15) return;
    let rating = null;
    const rm = whole.match(/([0-5](?:\.\d)?)\s*out of 5/i);
    if (rm) rating = parseFloat(rm[1]);
    const titleEl = n.querySelector('[class*="review-title"], h4, h3, [itemprop="name"]');
    const bodyEl = n.querySelector('[class*="ugc-review-body"], [class*="review-content"], [itemprop="reviewBody"], p');
    const authorEl = n.querySelector('[class*="reviewer-name"], [class*="ugc-author"], [itemprop="author"]');
    const dateEl = n.querySelector('time, [class*="submission"], [itemprop="datePublished"]');
    out.push({
      rating: rating,
      title: clean(titleEl && titleEl.innerText),
      text: clean(bodyEl && bodyEl.innerText),
      reviewer: clean(authorEl && authorEl.innerText),
      date: clean(dateEl && (dateEl.getAttribute('datetime') || dateEl.innerText)),
      verified: /verified purchase/i.test(whole)
    });
  });
  return out;
}
"""

# Count of review-ish nodes currently rendered in the DOM, used by
# _load_all_reviews to tell whether a "Show More Reviews" click actually
# added anything (same node selector as _JS_REVIEWS).
_JS_REVIEW_COUNT = r"""
() => document.querySelectorAll(
  'li.review-item, [class*="review-item"], .ugc-review, [data-testid*="review"] li, [itemprop="review"]'
).length
"""

# Best Buy's product page reviews are lazy-loaded: only the first batch is
# embedded in JSON-LD at page load, with the rest revealed by a "Show More
# Reviews"-style control (no separate review URL/tab was found; a proxy
# outage on product pages prevented inspecting the backing network request,
# so this clicks the visible control rather than assuming an unverified
# internal API shape). Scoped to a review-ish ancestor so it doesn't fire on
# an unrelated "Show more" control elsewhere on the page (e.g. specs).
_JS_CLICK_SHOW_MORE_REVIEWS = r"""
() => {
  const re = /show more review|load more review|see more review|more reviews|show more|load more/i;
  const btns = Array.from(document.querySelectorAll('button, a[role="button"], a'));
  for (const b of btns) {
    const t = (b.innerText || '').trim();
    if (!t || !re.test(t)) continue;
    let el = b, scoped = false;
    for (let d = 0; d < 6 && el; d++, el = el.parentElement) {
      const cls = (el.className || '').toString().toLowerCase();
      const id = (el.id || '').toLowerCase();
      if (cls.includes('review') || cls.includes('ugc') || id.includes('review')) { scoped = true; break; }
    }
    if (scoped) {
      b.scrollIntoView({block: 'center'});
      b.click();
      return true;
    }
  }
  return false;
}
"""


@dataclass
class EngineConfig:
    search_text: str
    marketplace: str = "bestbuy.com"
    exclude_keywords: list[str] = field(default_factory=list)
    max_pages: int = 1
    max_products: int = 20
    fetch_reviews: bool = True
    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 60_000
    debug_dir: str | None = None


class BestBuyEngine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.pages_processed = 0

    async def run(self) -> list[dict]:
        cfg = self.cfg
        _log.info("event=engine_start search=%r max_pages=%s max_products=%s headless=%s",
                  cfg.search_text, cfg.max_pages, cfg.max_products, cfg.headless)
        models: list[dict] = []
        launch_args = ["--disable-blink-features=AutomationControlled", "--start-maximized"]

        async with async_playwright() as pw:
            async def _on_context(c):
                try:
                    await c.add_cookies([
                        {"name": "intl_splash", "value": "false", "domain": ".bestbuy.com", "path": "/"},
                        {"name": "country_selection", "value": "US", "domain": ".bestbuy.com", "path": "/"},
                    ])
                    _log.info("event=region_cookies_injected")
                except Exception as cookie_exc:
                    _log.warning("event=cookie_injection_failed error=%s", cookie_exc)
                await c.add_init_script(_STEALTH)

            ctx, browser, route = await net_routing.open_routed_context(
                pw, "bestbuy", headless=cfg.headless, channel=cfg.browser_channel,
                profile_dir=cfg.chrome_profile_dir, user_agent=random_user_agent(UA),
                launch_args=launch_args, on_context=_on_context)
            _log.info("event=browser_launched route=%s", route.value)

            try:
                await self._prime_region(ctx)
                urls = await self._search(ctx)
                _log.info("event=discovery_done urls=%d", len(urls))
                for url in urls:
                    if cfg.max_products > 0 and len(models) >= cfg.max_products:
                        break
                    page = await ctx.new_page()
                    try:
                        model = await self._scrape_product(page, url)
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

    async def _search(self, ctx) -> list[str]:
        cfg = self.cfg
        excludes = [k.lower() for k in cfg.exclude_keywords if k]
        found: list[str] = []
        seen: set = set()
        page = await ctx.new_page()
        try:
            for page_no in range(1, max(1, cfg.max_pages) + 1):
                url = parsing.search_url(cfg.search_text, page_no)
                _log.info("event=search_page page=%d url=%s", page_no, url[:110])
                links, blocked = await self._load_search_links(page, url)
                if blocked:
                    _log.warning("event=search_blocked page=%d", page_no)
                    break
                self.pages_processed += 1
                added = 0
                for href in links:
                    pid = parsing.product_id_from_url(href)
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    found.append(href)
                    added += 1
                    if cfg.max_products > 0 and len(found) >= cfg.max_products:
                        break
                _log.info("event=search_extract page=%d added=%d total=%d", page_no, added, len(found))
                if cfg.max_products > 0 and len(found) >= cfg.max_products:
                    break
                if not links:
                    break
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return found

    async def _load_search_links(self, page: Page, url: str):
        cfg = self.cfg
        attempts = 3
        # Cap per-attempt navigation so an unreachable/timed-out Best Buy fails fast
        # instead of hanging the full nav timeout on every retry.
        goto_timeout = min(cfg.nav_timeout_ms, 30_000)
        for attempt in range(1, attempts + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout)
                if await self._ensure_us_region(page):
                    await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout)
                await page.wait_for_timeout(2500 + attempt * 1000)
                try:
                    # Wait specifically for real product-tile links (current
                    # alphanumeric-SKU "/product/" URLs, or legacy numeric
                    # ".p" URLs) rather than any "/site/" link: "/site/" also
                    # matches header/footer nav chrome (e.g. "/site/deals",
                    # "/site/trade-in/..."), which is already present before
                    # the result grid hydrates, so waiting on it resolves
                    # instantly and lets us scrape too early.
                    await page.wait_for_selector("a[href*='/product/'], a[href*='.p']", timeout=12000)
                except Exception:
                    pass
                # The result grid continues to hydrate/lazy-render for several
                # seconds after the first product link appears; scroll twice
                # with pauses so later tiles in the grid render before we read
                # the DOM (observed live: ~8-12s total needed via the proxy).
                await page.evaluate("window.scrollBy(0, 1200)")
                await page.wait_for_timeout(1800)
                await page.evaluate("window.scrollBy(0, 1200)")
                await page.wait_for_timeout(1800)
            except Exception as exc:
                _log.warning("event=search_page_failed attempt=%d/%d error=%s", attempt, attempts, exc)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                continue
            if _BLOCK_RE.search(await self._text(page)):
                return [], True
            links = await self._eval(page, _JS_LINKS, [], cfg.max_products * 3 if cfg.max_products else 60) or []
            if links:
                return links, False
            _log.info("event=search_empty_retry attempt=%d/%d", attempt, attempts)
            await asyncio.sleep(random.uniform(1.5, 2.5))
        _log.warning("event=bestbuy_unreachable_or_empty url=%s hint=%s", url[:90],
                     "bestbuy.com may be timing out / geo-blocked from this network; "
                     "a US IP (VPN/proxy) is usually required")
        return [], False

    async def _ensure_us_region(self, page: Page) -> bool:
        try:
            text = (await self._text(page)).lower()
        except Exception:
            text = ""
        is_splash = (
            "choose a country" in text
            or "select your country" in text
            or ("united states" in text and "canada" in text and "bestbuy" in text and len(text) < 4000)
        )
        if not is_splash:
            return False
        for sel in (
            "a.us-link",
            "a[href*='bestbuy.com/']:has-text('United States')",
            "a:has-text('United States')",
            "text=United States",
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=5000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1500)
                    _log.info("event=country_selected country=US")
                    return True
            except Exception:
                continue
        _log.warning("event=country_select_failed splash_detected_but_no_us_link")
        return False

    async def _scrape_product(self, page: Page, url: str) -> dict | None:
        cfg = self.cfg
        if not await self._goto_retry(page, url):
            return None
        if await self._ensure_us_region(page):
            await self._goto_retry(page, url)
        if _BLOCK_RE.search(await self._text(page)):
            if not await self._wait_manual_solve(page):
                _log.warning("event=product_blocked url=%s", url[:90])
                return None

        # Wait for the product title, then scroll so lazy specs/reviews render.
        try:
            await page.wait_for_selector("h1", timeout=12000)
        except Exception:
            pass
        await self._load_full_page(page)

        script_texts = await self._script_texts(page)
        blocks = parsing.parse_blocks(script_texts)
        info = parsing.product_from_blocks(blocks)
        ld_reviews = parsing.reviews_from_blocks(blocks) if cfg.fetch_reviews else []

        dom = await self._eval(page, _JS_DOM, {}) or {}
        if not info["product_name"] and dom.get("title"):
            info["product_name"] = parsing.clean_text(dom["title"])
        if not info["brand"] and dom.get("brand"):
            info["brand"] = parsing.clean_text(dom["brand"])
        if not info["description"] and dom.get("description"):
            info["description"] = parsing.clean_text(dom["description"])
        if info["price_amount"] is None and dom.get("priceText"):
            info["price_amount"] = parsing.parse_price(dom["priceText"])
        if info["original_price"] is None and dom.get("regularPriceText"):
            info["original_price"] = parsing.parse_price(dom["regularPriceText"])
        if not info["availability"]:
            avail = (dom.get("availabilityText") or "").lower()
            if "add to cart" in avail:
                info["availability"] = "in_stock"
            elif "sold out" in avail or "unavailable" in avail:
                info["availability"] = "out_of_stock"
        if info["average_rating"] is None and dom.get("ratingValue") is not None:
            info["average_rating"] = dom["ratingValue"]
        if info["total_reviews"] is None and dom.get("reviewCount") is not None:
            info["total_reviews"] = dom["reviewCount"]
        info["images"].extend(dom.get("images", []))
        info["images"] = parsing.dedupe_images(info["images"])
        for vid in dom.get("videos", []):
            if vid not in info["videos"]:
                info["videos"].append(vid)

        specs = await self._eval(page, _JS_SPECS, {}) or {}

        reviews = ld_reviews
        show_more_clicks = 0
        if cfg.fetch_reviews:
            show_more_clicks = await self._load_all_reviews(page)
            dom_reviews_raw = await self._eval(page, _JS_REVIEWS, []) or []
            dom_reviews = self._normalize_dom_reviews(dom_reviews_raw)
            if dom_reviews:
                reviews = parsing.merge_reviews(ld_reviews, dom_reviews)
            _log.info("event=reviews_collected ld=%d dom=%d merged=%d show_more_clicks=%d",
                      len(ld_reviews), len(dom_reviews), len(reviews), show_more_clicks)

        # Commercial-surface extras (item 3): manufacturer/warranty come from
        # the already-captured spec table (generic, defensive key search, see
        # parsing.extract_manufacturer/extract_warranty); delivery/trade-in
        # are read from the page's own visible text since no fixed CSS
        # selector for them could be live-confirmed this pass (proxy blocked
        # all product-page navigation - see report). All four are None/empty
        # when not actually present, never fabricated.
        page_text = await self._text(page)
        manufacturer = parsing.extract_manufacturer(specs, info["brand"])
        warranty = parsing.extract_warranty(specs, page_text)
        delivery = parsing.extract_delivery(page_text)
        trade_in = parsing.extract_trade_in(page_text)

        pid = parsing.product_id_from_url(url) or info.get("sku")
        return {
            "url": url.split("?")[0],
            "product_id": pid,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "product_name": info["product_name"],
            "brand": info["brand"],
            "description": info["description"],
            "model_number": info["model_number"] or info["sku"],
            "specifications": specs,
            "manufacturer": manufacturer,
            "warranty": warranty,
            "delivery": delivery,
            "trade_in": trade_in,
            "images": info["images"],
            "videos": info["videos"],
            "availability": info["availability"],
            "stock_quantity": info["stock_quantity"],
            "price": {"currency": "USD", "selling_price": info["price_amount"],
                      "original_price": info["original_price"]},
            "ratings": {"average_rating": info["average_rating"], "total_reviews": info["total_reviews"]},
            "reviews": reviews,
        }

    async def _prime_region(self, ctx) -> None:
        cfg = self.cfg
        page = await ctx.new_page()
        try:
            await page.goto("https://www.bestbuy.com/", wait_until="domcontentloaded",
                            timeout=min(cfg.nav_timeout_ms, 30_000))
            await page.wait_for_timeout(2500)
            await self._ensure_us_region(page)
        except Exception as exc:
            _log.warning("event=region_prime_failed error=%s", exc)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _load_full_page(self, page: Page) -> None:
        for _ in range(6):
            try:
                await page.evaluate("window.scrollBy(0, 1400)")
                await page.wait_for_timeout(700)
            except Exception:
                break
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(500)

    async def _load_all_reviews(self, page: Page) -> int:
        """Best Buy lazy-loads reviews behind a "Show More Reviews"-style
        control rather than a URL-paginated listing (no separate review
        page/tab was found, and a proxy outage on product pages prevented
        inspecting any backing network request - see report): click it
        repeatedly, re-checking the rendered review-node count each time, so
        every available review ends up in the DOM for _JS_REVIEWS to read.
        Stops when the control disappears, the count stops growing for two
        consecutive clicks, or a generous safety cap is hit (a ceiling, not
        an expected page count - consistent with this codebase's
        completeness-first review pagination elsewhere, e.g. Alibaba's
        next-button loop and Flipkart's page-by-page review listing).
        Returns the number of successful clicks, for logging."""
        SAFETY_CAP = 150
        clicks = 0
        prev_count = -1
        stagnant = 0
        for _ in range(SAFETY_CAP):
            count = await self._eval(page, _JS_REVIEW_COUNT, 0)
            if count == prev_count:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
            prev_count = count
            clicked = await self._eval(page, _JS_CLICK_SHOW_MORE_REVIEWS, False)
            if not clicked:
                break
            clicks += 1
            await page.wait_for_timeout(1200)
        return clicks

    @staticmethod
    def _normalize_dom_reviews(rows: list) -> list[dict]:
        out = []
        for r in rows or []:
            text = (r.get("text") or "").strip()
            if not text and r.get("rating") is None:
                continue
            out.append({
                "review_id": None,
                "rating": r.get("rating"),
                "title": (r.get("title") or "").strip() or None,
                "review_text": text or None,
                "reviewer_name": (r.get("reviewer") or "").strip() or None,
                "posted_date": parsing._norm_date(r.get("date")),
                "verified_purchase": bool(r.get("verified")),
                "helpful_count": None,
            })
        # DOM review cards carry no identifier, so duplicates (the same card
        # matched twice by overlapping selectors) are only catchable by the
        # composite key - same review_key used for the JSON-LD source.
        seen: set = set()
        deduped: list[dict] = []
        for r in out:
            k = parsing.review_key(r)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(r)
        return deduped

    async def _script_texts(self, page: Page) -> list[str]:
        texts: list[str] = []
        try:
            texts.extend(await page.locator('script[type="application/ld+json"]').all_inner_texts())
        except Exception:
            pass
        for sel in ("script#__NEXT_DATA__", 'script[id*="__NEXT_DATA__"]'):
            try:
                loc = page.locator(sel)
                if await loc.count():
                    texts.append(await loc.first.inner_text())
            except Exception:
                pass
        return texts

    async def _goto_retry(self, page: Page, url: str, attempts: int = 3) -> bool:
        for i in range(1, attempts + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                return True
            except Exception as exc:
                _log.warning("event=goto_retry attempt=%d/%d url=%s error=%s", i, attempts, url[:70], exc)
                await page.wait_for_timeout(1500)
        return False

    async def _wait_manual_solve(self, page: Page) -> bool:
        if self.cfg.headless:
            return False
        deadline = settings.captcha_wait_seconds
        _log.warning("event=captcha_wait seconds=%d msg=solve the challenge in the browser window", deadline)
        waited = 0
        while waited < deadline:
            await page.wait_for_timeout(5000)
            waited += 5
            try:
                if not _BLOCK_RE.search(await self._text(page)):
                    _log.info("event=captcha_cleared waited=%ds", waited)
                    return True
            except Exception:
                pass
        return False

    async def _text(self, page: Page) -> str:
        try:
            return await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    async def _eval(self, page: Page, script: str, default=None, *args):
        try:
            return await page.evaluate(script, *args)
        except Exception:
            return default