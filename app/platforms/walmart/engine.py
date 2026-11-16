from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field

from playwright.async_api import Page, async_playwright

from app.core.logging import get_logger
from app.platforms.walmart import parsing

_log = get_logger("walmart.engine")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""
_BLOCK_RE = re.compile(r"robot or human|verify your identity|verify you are|captcha|unusual traffic|press & hold", re.I)

_JS_CARDS = r"""
() => {
  const out = []; const seen = new Set();
  for (const link of document.querySelectorAll("a[href*='/ip/']")) {
    const href = link.href || "";
    const m = href.match(/\/ip\/(?:[^/?#]+\/)?(\d+)/);
    if (!m) continue;
    const id = m[1]; if (seen.has(id)) continue; seen.add(id);
    out.push({id, href, name: (link.innerText || "").trim()});
  }
  return out;
}
"""
_JS_MEDIA = r"""
() => {
  const img = [...document.querySelectorAll("img")].map(x => x.currentSrc || x.src).filter(x => x && x.startsWith("http"));
  const vid = [...document.querySelectorAll("video, video source")].map(x => x.currentSrc || x.src).filter(x => x && x.startsWith("http"));
  return {images: [...new Set(img)], videos: [...new Set(vid)]};
}
"""


@dataclass
class EngineConfig:
    search_text: str
    marketplace: str = "walmart.com"
    exclude_keywords: list[str] = field(default_factory=list)
    max_pages: int = 1
    max_products: int = 20
    fetch_reviews: bool = True
    review_sort: str = "submission-desc"
    review_pages_per_product: int = 1
    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 60_000
    debug_dir: str | None = None


class WalmartEngine:
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
            browser = None
            mode = "persistent_profile" if cfg.chrome_profile_dir else "fresh_context"
            _log.info("event=browser_launching mode=%s channel=%s headless=%s", mode, cfg.browser_channel, cfg.headless)
            try:
                if cfg.chrome_profile_dir:
                    ctx = await pw.chromium.launch_persistent_context(
                        cfg.chrome_profile_dir, channel=cfg.browser_channel, headless=cfg.headless,
                        args=launch_args, user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900})
                else:
                    browser = await pw.chromium.launch(headless=cfg.headless, channel=cfg.browser_channel, args=launch_args)
                    ctx = await browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900})
            except Exception as exc:
                _log.error("event=browser_launch_failed error=%s", exc)
                raise
            await ctx.add_init_script(_STEALTH)
            _log.info("event=browser_launched mode=%s", mode)

            try:
                products = await self._search(ctx)
                _log.info("event=discovery_done products=%d", len(products))
                for base in products:
                    if cfg.max_products > 0 and len(models) >= cfg.max_products:
                        break
                    page = await ctx.new_page()
                    try:
                        model = await self._scrape_product(page, base)
                        if model and model.get("product_name"):
                            models.append(model)
                            _log.info("event=product_done id=%s reviews=%d",
                                      model.get("product_id"), len(model.get("reviews", [])))
                    except Exception as exc:
                        _log.error("event=product_failed id=%s error=%s", base.get("product_id"), exc)
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

    async def _search(self, ctx) -> list[dict]:
        cfg = self.cfg
        excludes = [k.lower() for k in cfg.exclude_keywords if k]
        found: dict[str, dict] = {}
        page = await ctx.new_page()
        try:
            for page_no in range(1, max(1, cfg.max_pages) + 1):
                url = parsing.search_url(cfg.search_text, page_no)
                _log.info("event=search_page page=%d url=%s", page_no, url[:110])
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                    await page.wait_for_timeout(3000)
                except Exception as exc:
                    _log.warning("event=search_page_failed page=%d error=%s", page_no, exc)
                    continue
                body = await self._text(page)
                if _BLOCK_RE.search(body):
                    _log.warning("event=search_blocked page=%d (captcha/verification)", page_no)
                    break
                self.pages_processed += 1
                props = await self._next_data(page)
                items = parsing.search_items(props)
                bases = [parsing.base_product(i, cfg.search_text) for i in items]
                if not bases:
                    cards = await self._eval(page, _JS_CARDS, [])
                    bases = [{"product_id": str(c.get("id")), "product_name": c.get("name") or None,
                              "product_url": c.get("href"), "images": [], "search_query": cfg.search_text}
                             for c in cards if c.get("id")]
                added = 0
                for b in bases:
                    pid = b.get("product_id")
                    if not pid or pid in found:
                        continue
                    name = (b.get("product_name") or "").lower()
                    if excludes and any(k in name for k in excludes):
                        continue
                    found[pid] = b
                    added += 1
                    if cfg.max_products > 0 and len(found) >= cfg.max_products:
                        break
                _log.info("event=search_extract page=%d added=%d total=%d", page_no, added, len(found))
                if cfg.max_products > 0 and len(found) >= cfg.max_products:
                    break
                if not bases:
                    break
                await page.wait_for_timeout(1500)
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return list(found.values())

    async def _scrape_product(self, page: Page, base: dict) -> dict | None:
        cfg = self.cfg
        url = base.get("product_url")
        if not url:
            return None
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
        await page.wait_for_timeout(2500)
        body = await self._text(page)
        if _BLOCK_RE.search(body):
            _log.warning("event=product_blocked id=%s", base.get("product_id"))
            return None

        props = await self._next_data(page)
        product_data = ((props.get("initialData") or {}).get("data") or {}).get("product") or {}
        detail = parsing.detail_fields(product_data) if product_data else {}
        media = await self._eval(page, _JS_MEDIA, {"images": [], "videos": []}) or {"images": [], "videos": []}
        rating = parsing.rating_from_text(body)

        model = {
            "url": url,
            "product_id": base.get("product_id"),
            "scraped_at": _now(),
            "product_name": parsing.first_value(detail.get("product_name"), base.get("product_name")),
            "brand": parsing.first_value(detail.get("brand"), base.get("brand")),
            "description": detail.get("description"),
            "specifications": detail.get("specifications", {}),
            "model_number": detail.get("model_number"),
            "images": parsing.unique((detail.get("images") or []) + base.get("images", []) + media.get("images", [])),
            "videos": parsing.unique((detail.get("videos") or []) + media.get("videos", [])),
            "availability": parsing.product_availability_status(detail.get("availability")),
            "price": {
                "currency": "USD",
                "selling_price": parsing.first_value(detail.get("price_amount"), base.get("price_amount")),
                "original_price": detail.get("original_price"),
            },
            "ratings": {
                "average_rating": parsing.first_value(detail.get("average_rating"), base.get("average_rating"), rating.get("average_rating")),
                "total_reviews": parsing.first_value(detail.get("total_reviews"), base.get("total_reviews"), rating.get("total_reviews")),
            },
            "reviews": [],
        }

        if cfg.fetch_reviews:
            model["reviews"] = await self._scrape_reviews(page, base.get("product_id"))
        return model

    async def _scrape_reviews(self, page: Page, product_id: str) -> list[dict]:
        cfg = self.cfg
        collected: list[dict] = []
        seen: set = set()
        for page_no in range(1, max(1, cfg.review_pages_per_product) + 1):
            url = parsing.review_url(product_id, page_no, cfg.review_sort)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                await page.wait_for_timeout(2000)
            except Exception:
                break
            body = await self._text(page)
            if _BLOCK_RE.search(body):
                break
            props = await self._next_data(page)
            raws = parsing.find_review_array(props) if props else []
            parsed = [parsing.parse_review(r) for r in raws]
            new = 0
            for rev in parsed:
                key = rev.get("review_id") or (rev.get("reviewer_name"), rev.get("posted_date"), (rev.get("review_text") or "")[:60])
                if key in seen:
                    continue
                seen.add(key)
                collected.append(rev)
                new += 1
            if new == 0:
                break
            await asyncio.sleep(random.uniform(0.8, 1.5))
        return collected

    async def _next_data(self, page: Page) -> dict:
        try:
            loc = page.locator("script#__NEXT_DATA__")
            if await loc.count():
                return parsing.page_props(await loc.first.inner_text())
        except Exception as exc:
            _log.warning("event=next_data_failed error=%s", exc)
        return {}

    async def _text(self, page: Page) -> str:
        try:
            return await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    async def _eval(self, page: Page, script: str, default=None):
        try:
            return await page.evaluate(script)
        except Exception:
            return default


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
