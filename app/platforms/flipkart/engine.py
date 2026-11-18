from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from app.core.logging import get_logger
from app.platforms.browser import proxy_settings, random_user_agent
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
    review_pages_per_product: int = 1
    verified_purchase_only: bool = False
    headless: bool = False
    browser_channel: str = "chrome"
    chrome_profile_dir: str | None = None
    nav_timeout_ms: int = 60_000
    debug_dir: str | None = None


class FlipkartEngine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self.pages_processed = 0

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
            try:
                if cfg.chrome_profile_dir:
                    ctx = await pw.chromium.launch_persistent_context(
                        cfg.chrome_profile_dir, channel=cfg.browser_channel,
                        headless=cfg.headless, args=launch_args, user_agent=random_user_agent(UA),
                        locale="en-IN", viewport=viewport, proxy=proxy_settings())
                else:
                    browser = await pw.chromium.launch(
                        headless=cfg.headless, channel=cfg.browser_channel, args=launch_args,
                        proxy=proxy_settings())
                    ctx = await browser.new_context(user_agent=random_user_agent(UA), locale="en-IN", viewport=viewport)
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
                            models.append(data)
                            _log.info("event=product_done id=%s reviews=%d",
                                      data.get("product_id") or data.get("item_id"),
                                      len(data.get("reviews", [])))
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
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
                    await page.wait_for_timeout(2000)
                    await self._dismiss_login(page)
                    await self._scroll_page(page)
                except Exception as exc:
                    _log.warning("event=search_page_failed page=%d error=%s", page_no, exc)
                    continue
                body = await self._inner_text(page)
                if _BLOCK_RE.search(body):
                    _log.warning("event=search_blocked page=%d", page_no)
                    break
                self.pages_processed += 1
                results = await self._ev(page, scripts.JS_SEARCH_RESULTS, []) or []
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

    async def _scrape_product(self, page: Page, item: dict) -> dict | None:
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

        data["price"] = self._build_price(await self._ev(page, scripts.JS_PRICES, {}) or {}, text, offer)
        data["ratings"] = self._build_ratings(agg, text)
        data["offers"] = self._build_offers(text)
        data["delivery"], data["seller"], data["warranty_and_policies"] = self._build_logistics(text)

        features = await self._ev(page, scripts.JS_HIGHLIGHTS, []) or []
        if not features and prod.get("description"):
            features = [l.strip() for l in prod["description"].splitlines() if l.strip()]
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

        data["review_summary"] = {}
        data["reviews"] = []
        if cfg.fetch_reviews:
            try:
                summary, reviews = await self._scrape_reviews(page, data["url"])
                data["review_summary"] = summary
                data["reviews"] = reviews
            except Exception as exc:
                _log.error("event=reviews_failed url=%s error=%s", url[:90], exc)
        return data

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
        wow = re.search(r"Buy at\s*₹\s*([\d,]+)", text)
        fee = re.search(r"\+\s*₹\s*([\d,]+)\s*Protect Promise Fee", text, re.I)
        extra = re.search(r"Extra\s*₹\s*([\d,]+)\s*off", text, re.I)
        return {
            "currency": offer.get("priceCurrency", "INR"),
            "selling_price": selling,
            "mrp": mrp,
            "discount_percent": discount,
            "special_deal_price": parsing.to_number(wow.group(1)) if wow else None,
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
            "bank_offers": parsing.group_offers(bank_blk, r"^₹[\d,]+\s*off|^\d+%") or old_style,
            "emi_offers": parsing.group_offers(emi_blk, r"^₹[\d,]+/m"),
            "instant_loan_offers": parsing.group_offers(loan_blk, r"^₹[\d,]+/m"),
            "exchange_offer": " | ".join(l for l in exch_blk.splitlines() if l.strip() and l.strip() != "Apply"),
            "additional_benefits": parsing.group_offers(
                parsing.section(text, r"^Additional benefits on this purchase\s*$", [r"^Delivery details"]), r"^$"),
        }
        offers["no_cost_emi_mentioned"] = bool(re.search(r"no[\s-]*cost\s*emi", text, re.I))
        offers["coupon_mentioned"] = bool(re.search(r"\bcoupon\b", text, re.I))
        return offers

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
        source = "table"
        if not specs:
            block = await self._ev(page, scripts.JS_SPECS_TEXT, "") or ""
            if not block:
                block = parsing.section(
                    text, r"^Specifications\s*$",
                    [r"^Ratings & Reviews", r"^Questions and Answers", r"^Safe and Secure"])
            specs = parsing.parse_spec_text(block)
            source = "text"
        return {
            "specifications": specs,
            "specifications_flat": parsing.flatten_specs(specs),
            "specifications_parsed_from": source,
        }

    async def _scrape_reviews(self, page: Page, product_url: str):
        cfg = self.cfg
        review_url = parsing.build_review_url(product_url)
        await self._load_review_page(page, review_url)
        text = await self._inner_text(page)

        summary = parsing.parse_review_summary(text)
        summary["aspect_ratings"] = await self._scrape_aspects(page)
        summary["review_url"] = review_url

        await self._load_review_page(page, review_url)
        base = await self._apply_sort(page, review_url)

        seen: set = set()
        all_reviews: list = []
        empty_streak = 0
        stop_reason = "reached max pages"
        pages_done = 0
        max_pages = max(1, cfg.review_pages_per_product)

        for p in range(1, max_pages + 1):
            url = parsing.with_page(base, p)
            await self._load_review_page(page, url)
            page_text = await self._inner_text(page)
            if _BLOCK_RE.search(page_text):
                stop_reason = f"blocked on page {p}"
                break
            got = parsing.parse_reviews(page_text)
            if not got:
                await page.wait_for_timeout(3000)
                page_text = await self._inner_text(page)
                got = parsing.parse_reviews(page_text)
            pages_done = p

            new = 0
            for r in got:
                if cfg.verified_purchase_only and not r.get("verified_purchase"):
                    continue
                key = (r["reviewer_name"], r["rating"], r["title"], (r["review_text"] or "")[:80],
                       r["posted_relative"], r["helpful_count"])
                if key in seen:
                    continue
                seen.add(key)
                all_reviews.append(r)
                new += 1

            if not got:
                empty_streak += 1
                if empty_streak >= 2:
                    stop_reason = f"no reviews on pages {p - 1} and {p}"
                    break
            else:
                empty_streak = 0
            await asyncio.sleep(random.uniform(1.0, 2.0))

        summary["pages_scraped"] = pages_done
        summary["stop_reason"] = stop_reason
        summary["sort_used"] = cfg.review_sort
        return summary, all_reviews

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
