"""Alibaba scraping engine (Playwright).

Ported from the standalone scraper and refactored so that:
  * all tunables arrive through EngineConfig (no module-level globals, no
    user-supplied URLs — search is always keyword-built);
  * per-run state (supplier-feedback cache, concurrency guards) lives on the
    instance, so a long-lived worker can run many jobs without cross-talk;
  * it returns in-memory raw models instead of writing CSV/JSON/Mongo — output
    and normalization are the caller's responsibility.

The engine performs real browser automation against alibaba.com and never
bypasses login, CAPTCHA, or anti-bot controls; a CAPTCHA pauses the run for
manual solving in the visible browser.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page, async_playwright
from playwright_stealth import Stealth

from app.core.logging import get_logger
from app.platforms.alibaba import scripts
from app.platforms.alibaba.parsing import product_id_from_href

_log = get_logger("alibaba.engine")
_stealth = Stealth()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
]
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
]

_SORT_MAP = {
    "relevance": "relevance",
    "price_asc": "price-asc",
    "price_desc": "price-desc",
    "rating": "relevance",   # Alibaba trade search has no rating sort
    "newest": "relevance",   # nor a true "newest" sort
}
_PAGE_KEYS = ("pageno", "pagenum", "page", "currentpage", "pageindex",
              "beginpage", "curpage", "pagestart", "start")
_CAPTCHA_TEXTS = ["drag the slider", "unusual traffic from your network",
                  "please complete the security check", "nc_iconfont-load"]
_REVIEW_NEXT_SELS = (
    '[class*="review"] [class*="pagination"] [class*="next"]',
    '[class*="review-layout"] [aria-label*="Next" i]',
    '[class*="review"] button[aria-label*="Next" i]',
    '[class*="review"] [class*="pagination-next"]',
    '[class*="comet-pagination-next"]',
    '[class*="review"] a[aria-label*="Next" i]',
)
_FEEDBACK_BLOCK_RE = re.compile(
    r"adata\.alibaba|/[0-9]+\.gif|sc\.ma\.|\.gif(\?|$)|beacon|analytics|gtm|"
    r"doubleclick|log\.alibaba|mmstat|/wdm/|hm\.baidu",
    re.I,
)


@dataclass
class EngineConfig:
    search_text: str
    sort_by: str = "relevance"
    filter_verified: bool = False
    filter_trade_assurance: bool = False
    min_price: float | None = None
    max_price: float | None = None
    min_quantity: int | None = None
    include_sponsored: bool = True
    exclude_keywords: list[str] = field(default_factory=list)

    max_pages: int = 1
    max_products: int = 20
    fetch_reviews: bool = True
    review_concurrency: int = 3
    feedback_concurrency: int = 2
    feedback_max_pages: int = 80

    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 90_000
    captcha_wait_seconds: int = 300
    debug_dir: str | None = None


def _build_search_url(cfg: EngineConfig, page: int) -> str:
    params: dict[str, str] = {
        "fsb": "y",
        "IndexArea": "product_en",
        "SearchText": cfg.search_text,
    }
    if page > 1:
        params["page"] = str(page)
    filters = (["ta"] if cfg.filter_trade_assurance else []) + (
        ["verified"] if cfg.filter_verified else []
    )
    if filters:
        params["filter"] = "%2C".join(filters)
    if cfg.min_price is not None:
        params["minPrice"] = str(cfg.min_price)
    if cfg.max_price is not None:
        params["maxPrice"] = str(cfg.max_price)
    if cfg.min_quantity is not None:
        params["minQuantity"] = str(cfg.min_quantity)
    params["sortType"] = _SORT_MAP.get(cfg.sort_by, "relevance")
    return "https://www.alibaba.com/trade/search?" + urllib.parse.urlencode(params, safe="%")


def _raw_review_items(payload: dict) -> list:
    candidates = []

    def _walk(node, depth=0):
        if depth > 6 or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
        else:
            for key in ("feedbackList", "reviewList", "evaluateList",
                        "commentList", "rateList", "feedbacks"):
                val = node.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    candidates.append(val)
            for v in node.values():
                _walk(v, depth + 1)

    _walk(payload)
    if not candidates:
        return []
    return max(candidates, key=len)[:50]


def _parse_api_reviews(payload: dict) -> dict | None:
    candidates = []

    def _walk(node, depth=0):
        if depth > 6 or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
        else:
            for key in ("feedbackList", "reviewList", "evaluateList",
                        "commentList", "data", "result", "list", "items"):
                val = node.get(key)
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                    candidates.append((key, val))
            for v in node.values():
                _walk(v, depth + 1)

    _walk(payload)
    if not candidates:
        return None

    best = max(candidates, key=lambda kv: len(kv[1]))
    items = best[1]
    reviews = []
    for item in items[:50]:
        def s(k):
            return str(item.get(k, "N/A")).strip() or "N/A"

        raw_star = item.get("starNum") or item.get("star") or item.get("score") or item.get("rating")
        stars = str(raw_star) if raw_star is not None else "N/A"
        media = item.get("feedbackMedia") or item.get("images") or item.get("mediaList") or []
        imgs, vids = [], []
        for mo in media:
            if isinstance(mo, dict):
                u = mo.get("url") or mo.get("src") or mo.get("imageUrl") or mo.get("videoUrl")
            else:
                u = mo
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            is_video = (isinstance(mo, dict) and (mo.get("videoUrl") or str(mo.get("type", "")).lower() == "video")) \
                or re.search(r"\.(mp4|webm|mov|m3u8)(\?|$)", u, re.I)
            (vids if is_video else imgs).append(u)

        reviews.append({
            "reviewer": s("buyerName") if item.get("buyerName") else s("reviewerName") if item.get("reviewerName") else s("memberName") if item.get("memberName") else "N/A",
            "country": s("countryCode") if item.get("countryCode") else s("country") if item.get("country") else "N/A",
            "date": s("postDate") if item.get("postDate") else s("gmtCreate") if item.get("gmtCreate") else s("date") if item.get("date") else "N/A",
            "stars": stars,
            "text": s("feedback") if item.get("feedback") else s("content") if item.get("content") else s("reviewContent") if item.get("reviewContent") else s("comment") if item.get("comment") else "N/A",
            "model_number": s("skuInfo") if item.get("skuInfo") else s("goodsSku") if item.get("goodsSku") else s("sku") if item.get("sku") else "N/A",
            "verified_purchase": bool(item.get("verifiedPurchase") or item.get("isVerified")),
            "repeat_buyer": bool(item.get("repeatBuyer") or item.get("isRepeat")),
            "review_media": {"images": imgs, "videos": vids},
        })

    def _has_content(r):
        return (r["reviewer"] != "N/A" or r["text"] != "N/A" or r["date"] != "N/A"
                or r["stars"] != "N/A" or r["review_media"]["images"] or r["review_media"]["videos"])

    reviews = [r for r in reviews if _has_content(r)]
    if not reviews:
        return None

    overall = payload.get("avgStar") or payload.get("avgScore") or payload.get("totalStar") or "N/A"
    total = payload.get("totalCount") or payload.get("total") or len(reviews)
    return {
        "overall_rating": str(overall),
        "tabs": {},
        "reviews": reviews,
        "total_reviews": int(total) if str(total).isdigit() else len(reviews),
        "_source": "api",
    }


def _item_key(it: dict) -> str:
    for k in ("feedbackId", "id", "reviewId", "evaluateId", "gmtCreate", "feedbackTime"):
        v = it.get(k)
        if v:
            return f"{k}:{v}"
    return json.dumps(it, sort_keys=True)[:200]


def _next_request(url: str, post_data, page_no: int):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for k in list(qs):
        if k.lower() in _PAGE_KEYS:
            qs[k] = [str(page_no)]
            new_q = urllib.parse.urlencode({k: v[0] for k, v in qs.items()}, safe="%,")
            return urllib.parse.urlunparse(parsed._replace(query=new_q)), post_data
    if post_data:
        try:
            obj = json.loads(post_data)
            if isinstance(obj, dict):
                for k in list(obj):
                    if k.lower() in _PAGE_KEYS:
                        obj[k] = page_no
                        return url, json.dumps(obj)
        except Exception:
            pass
        if "=" in post_data:
            fqs = urllib.parse.parse_qs(post_data, keep_blank_values=True)
            for k in list(fqs):
                if k.lower() in _PAGE_KEYS:
                    fqs[k] = [str(page_no)]
                    return url, urllib.parse.urlencode({k: v[0] for k, v in fqs.items()})
    return None, None


def _dom_review_key(r: dict) -> tuple:
    return (str(r.get("reviewer", "")).strip(),
            str(r.get("date", "")).strip(),
            str(r.get("text", ""))[:60].strip())


def _dom_review_has_content(r: dict) -> bool:
    media = r.get("review_media", {})
    return (r.get("reviewer", "N/A") != "N/A" or r.get("text", "N/A") != "N/A"
            or r.get("date", "N/A") != "N/A" or r.get("stars", "N/A") != "N/A"
            or media.get("images") or media.get("videos"))


def _feedback_key(r: dict) -> tuple:
    return (str(r.get("reviewer", "")).strip(),
            str(r.get("date", "")).strip(),
            str(r.get("text", ""))[:60].strip())


def _feedback_first_id(items: list) -> str | None:
    for r in items:
        if not (r.get("reviewer", "N/A") == "N/A" and r.get("text", "N/A") == "N/A"):
            return f'{r.get("reviewer", "")}|{r.get("date", "")}'
    return None


def _classify_review_tab(label: str) -> str:
    low = label.lower()
    if any(k in low for k in ("store", "supplier", "company", "seller")):
        return "store_reviews"
    return "product_reviews"


def _build_model(r: dict, ts: str) -> dict:
    pid = product_id_from_href(r["href"])
    if r["verified"]:
        vstatus = "Verified"
    elif r["gold"]:
        vstatus = "Gold Supplier"
    else:
        vstatus = "Standard"
    return {
        "product_details": {
            "product_id": pid,
            "product_name": r["title"],
            "price_range": r["price"],
            "moq": r["moq"],
            "date_posted": r["date_posted"],
            "product_detail_url": r["href"],
            "scraped_at": ts,
            "media": {"images": r["images"], "videos": r["videos"]},
        },
        "reviews": {
            "product_reviews": {"product_id": pid, "rating": r["rating"], "review_count": r["review_count"]},
            "supplier_reviews": {
                "response_rate": r["response_rate"],
                "trade_assurance": r["trade_assurance"],
                "trust_score": r["trust_score"],
            },
            "detailed_reviews": None,
        },
        "supplier_details": {
            "card_name": r["supplier"],
            "verification_status": vstatus,
            "country": r["country"],
            "years_active": r["years_active"],
            "gold_supplier": r["gold"],
        },
        "is_sponsored": r.get("is_sponsored", False),
    }


class AlibabaEngine:
    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self.pages_processed = 0  # actual search-result pages processed
        self._feedback_cache: dict = {}
        self._feedback_inflight: dict = {}
        self._feedback_sem = asyncio.Semaphore(max(1, cfg.feedback_concurrency))

    # ── Browser helpers ──────────────────────────────────────────────────

    async def _new_context(self, browser):
        return await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport=random.choice(VIEWPORTS),
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    async def _new_page(self, ctx) -> Page:
        page = await ctx.new_page()
        await _stealth.apply_stealth_async(page)
        return page

    async def _wait_network_idle(self, page, timeout_ms: int = 3_000) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_ms / 1000
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        last, stable_since = -1, None
        while loop.time() < deadline:
            try:
                n = await page.evaluate("document.getElementsByTagName('*').length")
            except Exception:
                return
            if n == last:
                if stable_since is None:
                    stable_since = loop.time()
                elif (loop.time() - stable_since) * 1000 >= 500:
                    return
            else:
                last, stable_since = n, None
            await asyncio.sleep(0.15)

    async def _scroll_page(self, page: Page) -> None:
        height = await page.evaluate("document.body.scrollHeight") or 5000
        for i in range(1, 6):
            target = int(height * i / 5)
            await page.evaluate(f"window.scrollTo(0, {target})")
            await self._wait_network_idle(page, timeout_ms=2_000)
            new_h = await page.evaluate("document.body.scrollHeight") or height
            if new_h > height:
                height = new_h
        await page.evaluate("window.scrollTo(0, 0)")

    async def _has_captcha(self, page) -> bool:
        current_url = page.url
        if "_____tmd_____" in current_url or "/trade/search/verify" in current_url:
            return True
        try:
            if await page.locator("punish-component").count() > 0:
                return True
        except Exception:
            pass
        try:
            action = await page.evaluate("() => window._config_ && window._config_.action")
            if action == "captcha":
                return True
        except Exception:
            pass
        try:
            body_lower = (await page.inner_text("body")).lower()
            if any(t in body_lower for t in _CAPTCHA_TEXTS):
                return True
        except Exception:
            pass
        return False

    async def _wait_for_captcha_clear(self, page) -> None:
        if not await self._has_captcha(page):
            return
        _log.warning("event=captcha_detected url=%s", page.url[:90])
        await asyncio.sleep(4)
        _log.warning("event=captcha_manual_required message=solve the slider in the browser window")
        start = asyncio.get_event_loop().time()
        while True:
            if asyncio.get_event_loop().time() - start > self.cfg.captcha_wait_seconds:
                raise TimeoutError("Timed out waiting for manual CAPTCHA solving.")
            if not await self._has_captcha(page) and "_____tmd_____" not in page.url:
                break
            await asyncio.sleep(2)
        _log.info("event=captcha_cleared")

    # ── Search-page extraction ───────────────────────────────────────────

    async def _extract_page(self, page: Page) -> list[dict]:
        payload = await page.evaluate(scripts.EXTRACT_JS)
        items = payload["items"]
        if not self.cfg.include_sponsored:
            items = [r for r in items if not r.get("is_sponsored")]
        if self.cfg.exclude_keywords:
            lowered = [k.lower() for k in self.cfg.exclude_keywords if k]
            items = [r for r in items if not any(k in r["title"].lower() for k in lowered)]
        _log.info("event=dom_extract selector=%s containers=%s items=%d",
                  payload["selector"], payload["count"], len(items))
        ts = datetime.now(timezone.utc).isoformat()
        return [_build_model(r, ts) for r in items]

    # ── Review extraction ────────────────────────────────────────────────

    async def _wait_review_anchors(self, page) -> None:
        try:
            await page.wait_for_function(
                """() => {
                    const bt = document.body ? document.body.innerText : '';
                    return /verified\\s+purchase/i.test(bt) ||
                           /\\b[A-Za-z]\\*{2,}[a-z]?\\b/.test(bt);
                }""",
                timeout=12_000,
            )
        except Exception:
            pass

    async def _extract_current_reviews(self, page, api_captures):
        native_items: list = []
        feedback_cap = None
        for cap in api_captures:
            got = _raw_review_items(cap["body"])
            if got and len(got) > len(native_items):
                native_items = got
                feedback_cap = cap

        dom = await page.evaluate(scripts.REVIEWS_JS)
        date_posted = dom.get("date_posted", "N/A")
        reviews = [r for r in dom.get("reviews", []) if _dom_review_has_content(r)]
        if reviews:
            return ({"overall_rating": dom.get("overall_rating", "N/A"),
                     "total_reviews": len(reviews), "reviews": reviews,
                     "native_items": native_items, "_source": "dom"},
                    date_posted, feedback_cap)
        for cap in api_captures:
            parsed = _parse_api_reviews(cap["body"])
            if parsed and parsed["reviews"]:
                return ({"overall_rating": parsed["overall_rating"],
                         "total_reviews": parsed["total_reviews"],
                         "reviews": parsed["reviews"],
                         "native_items": native_items, "_source": "api"},
                        date_posted, feedback_cap)
        if native_items:
            return ({"overall_rating": "N/A", "total_reviews": len(native_items),
                     "reviews": [], "native_items": native_items,
                     "_source": "api_native"}, date_posted, feedback_cap)
        return (None, date_posted, feedback_cap)

    async def _collect_all_feedback(self, page, cap: dict, tab_count: int) -> list:
        collected: dict[str, dict] = {}
        for it in _raw_review_items(cap["body"]):
            collected[_item_key(it)] = it

        start_page = None
        parsed = urllib.parse.urlparse(cap["url"])
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for k, v in qs.items():
            if k.lower() in _PAGE_KEYS and v and v[0].isdigit():
                start_page = int(v[0])
                break
        if start_page is None and cap.get("post_data"):
            try:
                obj = json.loads(cap["post_data"])
                for k, v in (obj.items() if isinstance(obj, dict) else []):
                    if k.lower() in _PAGE_KEYS and str(v).isdigit():
                        start_page = int(v)
                        break
            except Exception:
                pass
        if start_page is None:
            start_page = 1

        headers = {k: v for k, v in (cap.get("headers") or {}).items()
                   if k.lower() not in ("content-length", "host", ":authority", ":method",
                                        ":path", ":scheme", "accept-encoding")}

        page_no = start_page
        empty_streak = 0
        for _ in range(self.cfg.feedback_max_pages):
            if tab_count and len(collected) >= tab_count:
                break
            page_no += 1
            new_url, new_post = _next_request(cap["url"], cap.get("post_data"), page_no)
            if new_url is None:
                break
            try:
                resp = await page.context.request.fetch(
                    new_url, method=cap.get("method", "GET"),
                    headers=headers, data=new_post, timeout=20_000)
                body = await resp.json()
            except Exception as e:
                _log.warning("event=feedback_page_failed page=%d error=%s", page_no, e)
                break
            items = _raw_review_items(body)
            new_added = 0
            for it in items:
                k = _item_key(it)
                if k not in collected:
                    collected[k] = it
                    new_added += 1
            if not items or new_added == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
        _log.info("event=feedback_paginated collected=%d tab_count=%d", len(collected), tab_count)
        return list(collected.values())

    async def _open_full_reviews(self, page) -> bool:
        sels = [
            'text=/view all[^<]{0,20}reviews/i',
            'text=/see all[^<]{0,20}reviews/i',
            'text=/view more reviews/i',
            'text=/all \\d[\\d,]* reviews/i',
            '[class*="review"] a:has-text("View all")',
            '[class*="review"] button:has-text("View all")',
        ]
        for sel in sels:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=3_000)
                await self._wait_network_idle(page, timeout_ms=5_000)
                await self._wait_review_anchors(page)
                return True
            except Exception:
                continue
        return False

    async def _paginate_review_dom(self, page, block: dict, tab_count: int) -> dict:
        if block is None:
            return block
        reviews = block.get("reviews", []) or []
        seen = {_dom_review_key(r) for r in reviews}
        for _ in range(40):
            if tab_count and len(reviews) >= tab_count:
                break
            clicked = False
            for sel in _REVIEW_NEXT_SELS:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0 or not await loc.is_enabled():
                        continue
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                    await loc.click(timeout=3_000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                break
            await self._wait_network_idle(page, timeout_ms=4_000)
            try:
                dom = await page.evaluate(scripts.REVIEWS_JS)
            except Exception:
                break
            added = 0
            for r in dom.get("reviews", []):
                if not _dom_review_has_content(r):
                    continue
                k = _dom_review_key(r)
                if k not in seen:
                    seen.add(k)
                    reviews.append(r)
                    added += 1
            if added == 0:
                break
        block["reviews"] = reviews
        block["total_reviews"] = len(reviews)
        return block

    async def _finalize_review_block(self, page, block: dict, feedback_cap, tab_count: int) -> dict:
        if block is None:
            return block
        if feedback_cap:
            full = await self._collect_all_feedback(page, feedback_cap, tab_count)
            if full:
                from app.platforms.alibaba.parsing import normalize_native_item
                block["native_items"] = full
                block["reviews"] = [normalize_native_item(x) for x in full]
                block["total_reviews"] = max(tab_count or 0, len(full))
                block["_source"] = "api_paginated"
                return block

        if tab_count and len(block.get("reviews", []) or []) < tab_count:
            if await self._open_full_reviews(page):
                try:
                    dom = await page.evaluate(scripts.REVIEWS_JS)
                    reviews = block.get("reviews", []) or []
                    seen = {_dom_review_key(r) for r in reviews}
                    for r in dom.get("reviews", []):
                        if _dom_review_has_content(r) and _dom_review_key(r) not in seen:
                            seen.add(_dom_review_key(r))
                            reviews.append(r)
                    block["reviews"] = reviews
                except Exception:
                    pass
        return await self._paginate_review_dom(page, block, tab_count)

    # ── Supplier feedback page ───────────────────────────────────────────

    async def _feedback_route(self, route):
        try:
            req = route.request
            if req.resource_type in ("image", "media", "font"):
                await route.abort()
                return
            if _FEEDBACK_BLOCK_RE.search(req.url):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    async def _next_is_disabled(self, nxt) -> bool:
        try:
            cls = (await nxt.get_attribute("class")) or ""
            aria = (await nxt.get_attribute("aria-disabled")) or ""
            return "disabled" in cls.lower() or aria == "true"
        except Exception:
            return False

    async def _dismiss_feedback_overlays(self, page) -> None:
        try:
            await page.evaluate("""
                () => {
                  const sels = ['#launcher','[id*="chat" i]','[class*="chat" i]',
                    '[class*="im-bar" i]','[class*="cookie" i]','[class*="consent" i]',
                    '[class*="gdpr" i]','[class*="contact-bar" i]','[class*="bottom-bar" i]',
                    '[class*="bottombar" i]','[class*="detail-fixed" i]','[class*="float-bar" i]'];
                  for (const s of sels) document.querySelectorAll(s).forEach(e => {
                    if (e.closest('.next-pagination')) return;
                    const st = getComputedStyle(e);
                    if (st.position === 'fixed' || st.position === 'sticky') e.style.display = 'none';
                  });
                }
            """)
        except Exception:
            pass

    async def _click_next_robust(self, page, nxt) -> bool:
        try:
            await nxt.scroll_into_view_if_needed(timeout=3_000)
        except Exception:
            pass
        await self._dismiss_feedback_overlays(page)
        for attempt in ("normal", "force", "js"):
            try:
                if attempt == "normal":
                    await nxt.click(timeout=5_000)
                elif attempt == "force":
                    await nxt.click(timeout=3_000, force=True)
                else:
                    await nxt.evaluate("el => el.click()")
                return True
            except Exception:
                continue
        return False

    async def _wait_feedback_change(self, page, prev_first_id, timeout_ms: int = 6_000) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            try:
                items = await page.evaluate(scripts.FEEDBACK_JS)
            except Exception:
                return False
            fid = _feedback_first_id(items)
            if fid and fid != prev_first_id:
                return True
            await asyncio.sleep(0.25)
        return False

    async def _fetch_store_feedback(self, ctx, feedback_url: str, tab_count: int) -> dict:
        page = await self._new_page(ctx)
        await page.route("**/*", self._feedback_route)
        collected: dict[tuple, dict] = {}
        termination = "page_cap"
        pages = 0
        try:
            await page.goto(feedback_url, wait_until="domcontentloaded", timeout=45_000)
            await self._wait_for_captcha_clear(page)

            async def _await_template(timeout_ms: int) -> bool:
                try:
                    await page.wait_for_function(scripts.HAS_REVIEW_ITEM_JS, timeout=timeout_ms)
                    return True
                except Exception:
                    return False

            if not await _await_template(15_000):
                try:
                    await self._scroll_page(page)
                except Exception:
                    pass
                if not await _await_template(8_000):
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=45_000)
                        await self._wait_for_captcha_clear(page)
                        await self._scroll_page(page)
                    except Exception:
                        pass
                    if not await _await_template(15_000):
                        _log.warning("event=feedback_no_template url=%s", feedback_url[:80])
                        return {"reviews": [], "complete": False,
                                "termination": "no_template", "pages": 0}

            no_new_streak = 0
            for _ in range(max(1, self.cfg.feedback_max_pages)):
                pages += 1
                try:
                    items = await page.evaluate(scripts.FEEDBACK_JS)
                except Exception:
                    termination = "extract_error"
                    break
                first_id = _feedback_first_id(items)
                added = 0
                for r in items:
                    if r.get("reviewer", "N/A") == "N/A" and r.get("text", "N/A") == "N/A":
                        continue
                    k = _feedback_key(r)
                    if k not in collected:
                        collected[k] = r
                        added += 1
                if added == 0:
                    no_new_streak += 1
                    if no_new_streak >= 2:
                        termination = "no_new_reviews"
                        break
                else:
                    no_new_streak = 0

                nxt = page.locator('.next-pagination button.next-next, '
                                   '.next-pagination .next-next button, '
                                   '.next-pagination button:has-text("Next")').first
                try:
                    cnt = await nxt.count()
                    enabled = (cnt > 0) and await nxt.is_enabled() and not await self._next_is_disabled(nxt)
                except Exception:
                    cnt, enabled = 0, False
                if cnt == 0 or not enabled:
                    termination = "next_disabled"
                    break
                if not await self._click_next_robust(page, nxt):
                    termination = "click_failed"
                    break
                if not await self._wait_feedback_change(page, first_id, timeout_ms=8_000):
                    await self._click_next_robust(page, nxt)
                    if not await self._wait_feedback_change(page, first_id, timeout_ms=8_000):
                        termination = "stale_after_click"
                        break

            complete = termination == "next_disabled"
            _log.info("event=feedback_extracted collected=%d pages=%d term=%s complete=%s",
                      len(collected), pages, termination, complete)
            return {"reviews": list(collected.values()), "complete": complete,
                    "termination": termination, "pages": pages}
        except Exception as e:
            _log.warning("event=feedback_failed url=%s error=%s", feedback_url[:70], e)
            return {"reviews": list(collected.values()), "complete": False,
                    "termination": "error", "pages": pages}
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _get_feedback(self, ctx, feedback_url: str, store_tab: int, company_id) -> dict:
        cid = company_id
        if cid is not None and cid in self._feedback_cache:
            return self._feedback_cache[cid]
        if cid is not None and cid in self._feedback_inflight:
            return await self._feedback_inflight[cid]

        fut = None
        if cid is not None:
            fut = asyncio.get_event_loop().create_future()
            self._feedback_inflight[cid] = fut
        try:
            async with self._feedback_sem:
                res = await self._fetch_store_feedback(ctx, feedback_url, store_tab)
            if cid is not None and res and res["reviews"] and res["termination"] not in (
                "error", "no_template", "click_failed", "stale_after_click", "extract_error"
            ):
                self._feedback_cache[cid] = res
            if fut is not None and not fut.done():
                fut.set_result(res)
            return res
        except Exception as e:
            if fut is not None and not fut.done():
                fut.set_exception(e)
            raise
        finally:
            if cid is not None:
                self._feedback_inflight.pop(cid, None)

    async def _fetch_reviews(self, ctx, href: str) -> dict:
        empty = {"date_posted": "N/A", "product_reviews": None, "store_reviews": None}
        page = await self._new_page(ctx)
        api_captures: list[dict] = []

        async def _on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                url = response.url
                hint = re.search(r"feedback|review|evaluat|comment|rate", url, re.I)
                if "json" not in ct and not hint:
                    return
                try:
                    body = await response.json()
                except Exception:
                    try:
                        txt = await response.text()
                        txt = re.sub(r"^[^{\[]*", "", txt).strip()
                        txt = re.sub(r"\)\s*;?\s*$", "", txt)
                        body = json.loads(txt)
                    except Exception:
                        return
                if not isinstance(body, dict):
                    return
                req = response.request
                try:
                    headers = await req.all_headers()
                except Exception:
                    headers = {}
                api_captures.append({
                    "url": response.url, "body": body, "method": req.method,
                    "headers": headers, "post_data": req.post_data,
                })
            except Exception:
                pass

        page.on("response", _on_response)
        result = {"date_posted": "N/A", "product_reviews": None, "store_reviews": None,
                  "seller": None, "store_review_summary": None, "product_media": None,
                  "product_ex": None, "product_review_summary": None}
        try:
            await asyncio.sleep(random.uniform(1.2, 2.5))
            await page.goto(href, wait_until="domcontentloaded", timeout=45_000)
            await self._wait_network_idle(page, timeout_ms=4_000)
            await self._wait_for_captcha_clear(page)

            try:
                sj = await page.evaluate(scripts.SELLER_JS)
                if sj:
                    result["seller"] = sj.get("seller")
                    result["store_review_summary"] = sj.get("store_review_summary")
                    result["product_media"] = sj.get("product_media")
                    result["product_ex"] = sj.get("product_ex")
                    result["product_review_summary"] = sj.get("product_review_summary")
            except Exception as e:
                _log.warning("event=seller_extract_failed error=%s", e)

            tabs: list[tuple[str, int, object]] = []
            for sel in ('[role="tab"]', "li", "a", "button", "span", "div"):
                try:
                    for loc in await page.locator(sel).all():
                        txt = (await loc.inner_text()).strip()
                        m = re.search(r"(.+?)\s*\((\d+)\)", txt)
                        if m and re.search(r"review|feedback|evaluate|store|product|supplier", m.group(1), re.I):
                            cnt = int(m.group(2))
                            if cnt > 0:
                                tabs.append((m.group(1).strip(), cnt, loc))
                except Exception:
                    pass
                if tabs:
                    break

            if not tabs:
                await self._wait_review_anchors(page)
                block, date, fb = await self._extract_current_reviews(page, api_captures)
                block = await self._finalize_review_block(page, block, fb, 0)
                result["date_posted"] = date
                if block:
                    block["tab_label"] = "default"
                    result["product_reviews"] = block
                return result

            uniq: dict[str, tuple[str, int, object]] = {}
            for label, cnt, loc in tabs:
                key = label.lower()
                if key not in uniq or cnt > uniq[key][1]:
                    uniq[key] = (label, cnt, loc)

            for label, cnt, loc in uniq.values():
                rtype = _classify_review_tab(label)
                try:
                    api_captures.clear()
                    await loc.scroll_into_view_if_needed()
                    await loc.click()
                    await self._wait_network_idle(page, timeout_ms=6_000)
                    await self._wait_review_anchors(page)
                except Exception as e:
                    _log.warning("event=tab_click_failed label=%r error=%s", label, e)
                    continue

                block, date, fb = await self._extract_current_reviews(page, api_captures)
                block = await self._finalize_review_block(page, block, fb, cnt)
                if date and date != "N/A":
                    result["date_posted"] = date
                if block:
                    block["tab_label"] = label
                    block["tab_count"] = cnt
                    existing = result[rtype]
                    if existing is None or block["total_reviews"] > existing["total_reviews"]:
                        result[rtype] = block

            seller = result.get("seller") or {}
            fb_url = seller.get("feedbackUrl")
            cid = seller.get("companyId")
            store_bk = result.get("store_reviews")
            store_tab = (store_bk or {}).get("tab_count", 0) or 0
            if fb_url and self.cfg.feedback_max_pages > 0 and (store_tab or store_bk):
                try:
                    fb_res = await self._get_feedback(ctx, fb_url, store_tab, cid)
                except Exception as e:
                    _log.warning("event=feedback_augment_failed error=%s", e)
                    fb_res = None
                if fb_res and fb_res["reviews"]:
                    if store_bk is None:
                        store_bk = {"overall_rating": "N/A", "tab_count": store_tab,
                                    "tab_label": "Store reviews"}
                        result["store_reviews"] = store_bk
                    if len(fb_res["reviews"]) >= len(store_bk.get("reviews", []) or []):
                        store_bk["reviews"] = copy.deepcopy(fb_res["reviews"])
                        store_bk["total_reviews"] = len(fb_res["reviews"])
                        store_bk["_source"] = "feedback_page"
                        store_bk["feedback_complete"] = fb_res["complete"]
                        store_bk["feedback_termination"] = fb_res["termination"]
                        store_bk["product_tab_count"] = store_tab

            return result
        except Exception as e:
            _log.error("event=review_failed url=%s error=%s", href[:70], e)
            return empty
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ── Orchestration ────────────────────────────────────────────────────

    async def run(self) -> list[dict]:
        cfg = self.cfg
        all_models: list[dict] = []
        total = 0
        page_limit = cfg.max_pages if cfg.max_pages > 0 else 10_000

        _log.info(
            "event=engine_start search=%r sort=%s max_pages=%s max_products=%s "
            "fetch_reviews=%s headless=%s channel=%s",
            cfg.search_text, cfg.sort_by, cfg.max_pages, cfg.max_products,
            cfg.fetch_reviews, cfg.headless, cfg.browser_channel,
        )

        async with async_playwright() as pw:
            launch_args = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
            browser = None
            mode = "persistent_profile" if cfg.chrome_profile_dir else "fresh_context"
            _log.info(
                "event=browser_launching mode=%s channel=%s headless=%s profile=%s",
                mode, cfg.browser_channel, cfg.headless, cfg.chrome_profile_dir,
            )
            try:
                if cfg.chrome_profile_dir:
                    ctx = await pw.chromium.launch_persistent_context(
                        cfg.chrome_profile_dir, channel=cfg.browser_channel,
                        headless=cfg.headless, args=launch_args)
                else:
                    browser = await pw.chromium.launch(
                        headless=cfg.headless, channel=cfg.browser_channel, args=launch_args)
                    ctx = await self._new_context(browser)
            except Exception as exc:
                _log.error(
                    "event=browser_launch_failed channel=%s headless=%s error=%s",
                    cfg.browser_channel, cfg.headless, exc,
                )
                raise
            _log.info("event=browser_launched mode=%s", mode)

            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
                Object.defineProperty(navigator, 'plugins',    { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages',  { get: () => ['en-US','en'] });
                window.chrome = { runtime: {} };
            """)

            try:
                for page_num in range(1, page_limit + 1):
                    url = _build_search_url(cfg, page_num)
                    _log.info("event=page_start page=%d url=%s", page_num, url[:90])
                    page = await self._new_page(ctx)
                    try:
                        if page_num == 1:
                            await page.goto("https://www.alibaba.com", wait_until="load", timeout=60_000)
                            await self._wait_network_idle(page, timeout_ms=4_000)
                            if await self._has_captcha(page):
                                await self._wait_for_captcha_clear(page)
                            await asyncio.sleep(random.uniform(0.8, 1.5))

                        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                        await self._wait_network_idle(page, timeout_ms=8_000)
                        if await self._has_captcha(page):
                            await self._wait_for_captcha_clear(page)
                            await self._wait_network_idle(page, timeout_ms=8_000)

                        try:
                            link_count = await page.evaluate("document.querySelectorAll('a[href]').length")
                            if link_count < 10:
                                await page.reload(wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                                await self._wait_network_idle(page, timeout_ms=8_000)
                                if await self._has_captcha(page):
                                    await self._wait_for_captcha_clear(page)
                        except Exception:
                            pass

                        card_sels = (
                            '.m-gallery-product-item-v2,[class*="search-card-e-offer"],'
                            '[class*="J-offer-wrapper"],[data-content="product-item"],'
                            '[class*="offer-list-item"],[class*="organic-list-offer"],'
                            '[class*="AlbCard"],[class*="offer-wrapper"],[class*="card-info"],'
                            '[class*="product-card"],a[href*="/product-detail/"],a[href*="product_"]'
                        )
                        try:
                            await page.wait_for_selector(card_sels, timeout=60_000)
                        except Exception:
                            _log.warning("event=cards_not_found page=%d", page_num)
                            self._save_debug(page, page_num)

                        await self._scroll_page(page)
                        models = await self._extract_page(page)

                        if cfg.max_products > 0 and models:
                            remaining = cfg.max_products - total
                            models = models[:remaining] if remaining > 0 else []

                        if cfg.fetch_reviews and models:
                            await self._fetch_reviews_for(ctx, models)
                    except Exception as exc:
                        _log.error("event=page_error page=%d error=%s", page_num, exc)
                        models = []
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass

                    if not models:
                        _log.warning("event=no_products page=%d stopping", page_num)
                        break

                    all_models.extend(models)
                    total += len(models)
                    self.pages_processed += 1
                    _log.info("event=page_done page=%d products=%d total=%d", page_num, len(models), total)

                    if cfg.max_products > 0 and total >= cfg.max_products:
                        break
                    if (cfg.max_pages == 0) or (page_num < cfg.max_pages):
                        await asyncio.sleep(random.uniform(2.5, 4.5))
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

        _log.info("event=engine_done products=%d", len(all_models))
        return all_models

    async def _fetch_reviews_for(self, ctx, models: list[dict]) -> None:
        sem = asyncio.Semaphore(max(1, self.cfg.review_concurrency))

        async def _one(m, slot_idx: int):
            if slot_idx > 0:
                await asyncio.sleep(random.uniform(0.6, 1.2) * slot_idx)
            async with sem:
                detail_url = m["product_details"]["product_detail_url"]
                rv = await self._fetch_reviews(ctx, detail_url)
                m["reviews"]["detailed_reviews"] = rv
                m["seller_native"] = rv.get("seller")
                m["store_review_summary"] = rv.get("store_review_summary")
                m["product_ex"] = rv.get("product_ex")
                m["product_review_summary_native"] = rv.get("product_review_summary")
                pm = rv.get("product_media") or {}
                if pm.get("images"):
                    m["product_details"]["media"]["images"] = pm["images"]
                if pm.get("videos"):
                    m["product_details"]["media"]["videos"] = pm["videos"]
                detail_date = rv.get("date_posted", "N/A")
                if detail_date and detail_date != "N/A":
                    m["product_details"]["date_posted"] = detail_date
                await asyncio.sleep(random.uniform(1.0, 2.0))

        await asyncio.gather(*[_one(m, i) for i, m in enumerate(models)])

    def _save_debug(self, page, page_num: int) -> None:
        if not self.cfg.debug_dir:
            return
        try:
            d = Path(self.cfg.debug_dir)
            d.mkdir(parents=True, exist_ok=True)
            asyncio.create_task(page.screenshot(path=str(d / f"search_page{page_num}.png")))
        except Exception:
            pass
