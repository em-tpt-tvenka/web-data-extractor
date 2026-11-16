from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from playwright.async_api import Page, async_playwright

from app.core.logging import get_logger
from app.platforms.bestbuy import parsing

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
  const anchors = Array.from(document.querySelectorAll('a[href*=".p"], a[href*="/product/"], a[href*="/site/"]'));
  const seen = new Set(); const out = [];
  for (const a of anchors) {
    const href = a.href || "";
    if (href && (href.includes('.p') || href.includes('/product/') || href.includes('skuId='))) {
      const m = href.match(/(\d{5,})/);
      if (m) {
        const sku = m[1];
        if (!seen.has(sku)) { seen.add(sku); out.push(href); if (out.length >= maxProducts) break; }
      }
    }
  }
  return out;
}
"""
_JS_DOM = r"""
() => {
  const titleEl = document.querySelector('h1.heading-5, h1[class*="heading"], h1');
  const priceEl = document.querySelector('.priceView-hero-price span[aria-hidden="true"], [class*="priceView-customer-price"]');
  const images = [];
  document.querySelectorAll('img[src*="bbystatic.com"], img[src*="bestbuy.com"]').forEach(img => {
    let src = img.src || img.currentSrc || img.getAttribute('data-src');
    if (src && !src.includes('icon') && !src.includes('logo') && !src.includes('svg')) {
      src = src.split('?')[0];
      if (!images.includes(src)) images.push(src);
    }
  });
  return {title: titleEl ? titleEl.innerText : null, priceText: priceEl ? priceEl.innerText : null, images};
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
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                    await page.wait_for_timeout(3500)
                    await page.evaluate("window.scrollBy(0, 1200)")
                    await page.wait_for_timeout(1500)
                except Exception as exc:
                    _log.warning("event=search_page_failed page=%d error=%s", page_no, exc)
                    continue
                if _BLOCK_RE.search(await self._text(page)):
                    _log.warning("event=search_blocked page=%d", page_no)
                    break
                self.pages_processed += 1
                links = await self._eval(page, _JS_LINKS, [], cfg.max_products * 3 if cfg.max_products else 60)
                added = 0
                for href in links or []:
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

    async def _scrape_product(self, page: Page, url: str) -> dict | None:
        cfg = self.cfg
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
        await page.wait_for_timeout(3000)
        if _BLOCK_RE.search(await self._text(page)):
            _log.warning("event=product_blocked url=%s", url[:90])
            return None

        script_texts = await self._script_texts(page)
        blocks = parsing.parse_blocks(script_texts)
        info = parsing.product_from_blocks(blocks)
        reviews = parsing.reviews_from_blocks(blocks) if cfg.fetch_reviews else []

        dom = await self._eval(page, _JS_DOM, {}) or {}
        if not info["product_name"] and dom.get("title"):
            info["product_name"] = parsing.clean_text(dom["title"])
        if info["price_amount"] is None and dom.get("priceText"):
            info["price_amount"] = parsing.parse_price(dom["priceText"])
            if info["price_amount"] is not None and not info["availability"]:
                info["availability"] = "in_stock"
        for img in dom.get("images", []):
            if img not in info["images"]:
                info["images"].append(img)

        pid = parsing.product_id_from_url(url) or info.get("sku")
        return {
            "url": url.split("?")[0],
            "product_id": pid,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "product_name": info["product_name"],
            "brand": info["brand"],
            "description": info["description"],
            "model_number": info["model_number"] or info["sku"],
            "specifications": {},
            "images": info["images"],
            "videos": info["videos"],
            "availability": info["availability"],
            "price": {"currency": "USD", "selling_price": info["price_amount"], "original_price": info["original_price"]},
            "ratings": {"average_rating": info["average_rating"], "total_reviews": info["total_reviews"]},
            "reviews": reviews,
        }

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
