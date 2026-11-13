"""
Optimized Playwright Browser Scraper for Samsung India Laptops.
Leverages Headless Chromium with aggressive network interception (blocking analytics and ads),
rendering dynamic single-page content, and extracting complete specifications,
readable introduction dates, MIME media objects, and user reviews.
"""

import logging
import requests
from typing import Dict, Any, Optional, Tuple
from playwright.sync_api import sync_playwright, Browser, BrowserContext

from models import extract_flat_summary
from scraper.filters import format_price_str
from scraper.platforms import scrape_product_and_reviews

logger = logging.getLogger(__name__)

# URL patterns to block for maximum speed and lower network latency
BLOCKED_RESOURCE_TYPES = ["font", "image_ad"]
BLOCKED_DOMAINS = [
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "criteo.com",
    "facebook.net",
    "hotjar.com",
    "demdex.net",
    "omtrdc.net",
    "tiqcdn.com"
]


def expand_pdp_sections(page) -> None:
    """Scrolls and expands specification accordions so specs and No Cost EMI offers render."""
    try:
        page.evaluate("window.scrollBy(0, 600);")
        page.wait_for_timeout(600)
        for sel in (
            "button:has-text('Expand all')",
            ".pdd32-product-spec__expand-cta button",
            "button:has-text('View All Specs')",
            "button:has-text('See all specifications')",
            "[class*='spec'] button:has-text('View more')",
        ):
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click(timeout=1500)
                    page.wait_for_timeout(600)
                    break
            except Exception:
                pass
    except Exception:
        pass


class PlaywrightSamsungScraper:
    """
    High-performance headless browser scraper using Playwright Chromium.
    Reuses a single browser context across multiple product pages.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._http_session: Optional[requests.Session] = None

    def start(self):
        """Starts Playwright browser instance with optimized routing."""
        logger.info("Initializing Playwright browser engine...")
        self._playwright = sync_playwright().start()
        used_firefox = False
        try:
            self._browser = self._playwright.firefox.launch(
                headless=self.headless
            )
            used_firefox = True
            logger.info("✓ Playwright Firefox engine started (bypasses Browser Security Plus).")
        except Exception as e_ff:
            logger.warning(f"Firefox launch failed ({e_ff}), falling back to Chromium...")
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                ignore_default_args=["--disable-extensions"],
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu"
                ]
            )
        context_kwargs = {"viewport": {"width": 1920, "height": 1080}}
        if not used_firefox:
            context_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        self._context = self._browser.new_context(**context_kwargs)
        # Block slow third-party trackers and ad networks for 3x speedup
        self._context.route("**/*", self._intercept_route)
        self._http_session = requests.Session()
        logger.info("Playwright browser context ready.")

    def _intercept_route(self, route):
        """Aborts unnecessary third-party tracking scripts to maximize speed."""
        req_url = route.request.url.lower()
        if any(domain in req_url for domain in BLOCKED_DOMAINS):
            route.abort()
        else:
            route.continue_()

    def scrape_product_and_reviews(
        self,
        url: str,
        days_cutoff: Optional[int] = 365,
        platform: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Opens a product URL in Playwright, extracts full Schema v2.0.0 product + review docs."""
        if not self._context:
            self.start()

        page = self._context.new_page()
        try:
            logger.info(f"Playwright navigating to: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            expand_pdp_sections(page)
            page.wait_for_timeout(600)

            html = page.content()
            prod_v2, rev_v2, error_reason = scrape_product_and_reviews(
                url=url,
                platform=platform,
                session=self._http_session,
                html_override=html,
                days_cutoff=days_cutoff,
            )
            if error_reason or not prod_v2:
                raise RuntimeError(error_reason or "Failed to extract product data.")

            flat = extract_flat_summary(prod_v2, rev_v2)
            media_info = f"{flat['images_count']} images, {flat['videos_count']} videos"
            p_display = format_price_str(flat["price_details"])
            logger.info(
                f"✓ [Playwright/{flat['platform']}] [{flat['product_id']}] '{flat['title'][:36]}...' | "
                f"Brand: {flat['brand']} | Cat: {flat['category']} | "
                f"Intro: {flat['introduced_date']} | Price: {p_display} | "
                f"Media: {media_info} | Reviews: {flat['total_reviews']}"
            )
            return prod_v2, rev_v2

        finally:
            page.close()

    def scrape_laptop_page(
        self,
        url: str,
        days_cutoff: Optional[int] = 365,
        platform: str = "auto",
    ) -> Dict[str, Any]:
        """Backward-compatible wrapper returning the Schema v2.0.0 Product Details document."""
        prod_v2, _ = self.scrape_product_and_reviews(url=url, days_cutoff=days_cutoff, platform=platform)
        return prod_v2

    scrape_product_page = scrape_laptop_page

    def close(self):
        """Gracefully closes Playwright browser and context."""
        if self._http_session:
            self._http_session.close()
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        logger.info("Playwright browser closed.")

