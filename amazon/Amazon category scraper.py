from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator, FormatChecker

log = logging.getLogger("amazon_scraper")

# ======================================================================================
#  SETTINGS - change these numbers, then just press Run in PyCharm
# ======================================================================================
MAX_PRODUCTS_PER_CATEGORY = 5     # products saved per category (5 HP laptops, 5 ACs, 5 fridges)
SEARCH_PAGES = 1                  # Amazon search result pages to look through (1 = page one only)
REVIEW_PAGES_PER_PRODUCT = 1      # extra review pages per product (0 = product-page reviews only)
REFRESH_SAVED_PRODUCTS = True     # True = re-scrape products already in the files
CATEGORY_TO_RUN = "all"           # "all", "hp_laptops", "air_conditioners" or "refrigerators"
MIN_DELAY_SECONDS = 2.0           # wait between page loads (random between min and max)
MAX_DELAY_SECONDS = 4.0
# Anything typed on the command line (e.g. --max-products 10) overrides these settings.

MARKETPLACES = {
    "amazon.in": {"marketplace": "Amazon India", "country": "India", "currency": "INR",
                  "accept_language": "en-IN,en;q=0.9", "timezone": "Asia/Kolkata"},
    "amazon.com": {"marketplace": "Amazon US", "country": "United States", "currency": "USD",
                   "accept_language": "en-US,en;q=0.9", "timezone": "America/New_York"},
}


USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "£": "GBP", "€": "EUR"}


class BlockedError(Exception):
    pass


def looks_blocked(html: str) -> bool:
    return any(s in html for s in (
        "/errors/validateCaptcha", "Enter the characters you see below",
        "api-services-support@amazon.com", "Type the characters you see in this image"))


def ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("Installing Playwright (one-time)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
        from playwright.sync_api import sync_playwright
    return sync_playwright


class Browser:
    """A real Chromium window that loads Amazon pages the way a person would."""

    def __init__(self, domain, delay=(3.0, 7.0), retries=3, headless=False,
                 profile_dir=".amazon_browser_profile", cdp_url=None):
        self.domain, self.delay, self.retries, self.headless = domain, delay, retries, headless
        lang = MARKETPLACES.get(domain, {}).get("accept_language", "en-US,en;q=0.9")
        self._last = 0.0
        sync_playwright = ensure_playwright()
        self._pw = sync_playwright().start()
        self._cdp = bool(cdp_url)
        if cdp_url:
            # attach to a Chrome window the user already opened (and signed in to)
            log.info("Connecting to your open Chrome at %s", cdp_url)
            self._remote = self._pw.chromium.connect_over_cdp(cdp_url)
            self._ctx = self._remote.contexts[0] if self._remote.contexts else self._remote.new_context()
            self.page = self._ctx.new_page()
            self.open_home()
            return
        opts = dict(
            user_data_dir=str(Path(profile_dir).resolve()), headless=headless,
            locale=lang.split(",")[0], timezone_id=MARKETPLACES.get(domain, {}).get("timezone", "UTC"),
            viewport={"width": 1366, "height": 900}, user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**opts)
        except Exception as e:  # noqa: BLE001
            if "Executable doesn't exist" not in str(e) and "playwright install" not in str(e):
                raise
            log.info("Installing Chromium for Playwright (one-time download)...")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            self._ctx = self._pw.chromium.launch_persistent_context(**opts)
        self._ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.open_home()

    @property
    def url(self) -> str:
        return self.page.url

    def open_home(self):
        home = f"https://www.{self.domain}/"
        log.info("Opening %s", home)
        self.page.goto(home, wait_until="domcontentloaded", timeout=60_000)
        self._resolve_captcha()
        for sel in ("#sp-cc-accept", "input[data-action-type='DISMISS']"):
            try:
                if self.page.locator(sel).first.is_visible(timeout=1500):
                    self.page.locator(sel).first.click()
            except Exception:  # noqa: BLE001
                pass
        self.page.wait_for_timeout(random.randint(1500, 3000))

    # ---- sign-in (needed only for full review pages) -----------------------------------
    def is_logged_in(self) -> bool:
        try:
            t = self.page.locator("#nav-link-accountList-nav-line-1").first.inner_text(timeout=3000)
            return "sign in" not in t.lower()
        except Exception:  # noqa: BLE001
            return False

    def ensure_login(self, wait_s=300) -> bool:
        if self.is_logged_in():
            log.info("Signed in to Amazon.")
            return True
        if self.headless:
            log.warning("--login needs a visible window; run without --headless.")
            return False
        log.warning("Please sign in to Amazon in the browser window (waiting up to %d min)...", wait_s // 60)
        try:
            self.page.locator("#nav-link-accountList").first.click()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.time() + wait_s
        while time.time() < deadline:
            self.page.wait_for_timeout(3000)
            if "/ap/" not in self.page.url and self.is_logged_in():
                log.info("Signed in. The browser profile will remember this.")
                return True
        log.warning("Not signed in; only reviews shown on product pages will be collected.")
        return False

    # ---- loading pages -------------------------------------------------------------------
    def _resolve_captcha(self):
        if not looks_blocked(self.page.content()):
            return
        try:
            btn = self.page.locator("button:has-text('Continue shopping')").first
            if btn.is_visible(timeout=1500):
                btn.click()
                self.page.wait_for_load_state("domcontentloaded")
                self.page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass
        if not looks_blocked(self.page.content()):
            return
        if self.headless:
            raise BlockedError("captcha page (run without --headless to solve it by hand)")
        log.warning("Amazon is showing a CAPTCHA. Solve it in the browser window; waiting up to 3 minutes...")
        deadline = time.time() + 180
        while time.time() < deadline:
            self.page.wait_for_timeout(2000)
            if not looks_blocked(self.page.content()):
                log.info("CAPTCHA cleared, continuing.")
                return
        raise BlockedError("captcha not solved in time")

    def _scroll(self, full=True):
        """Scroll like a reader so lazy sections (offers, specs, reviews, Q&A) load."""
        steps = random.randint(8, 12) if full else random.randint(3, 5)
        for _ in range(steps):
            self.page.mouse.wheel(0, random.randint(700, 1200))
            self.page.wait_for_timeout(random.randint(300, 700))
        # open collapsed "Product information" / "Technical details" sections if present
        for sel in ("#productDetails_expanderTables_depthLeftSections a.a-expander-header",
                    "#productDetails_expanderTables_depthRightSections a.a-expander-header",
                    "a.a-expander-header[aria-expanded='false']"):
            try:
                for el in self.page.locator(sel).all()[:8]:
                    if el.is_visible():
                        el.click(timeout=1500)
            except Exception:  # noqa: BLE001
                pass
        self.page.wait_for_timeout(800)

    def _wait(self):
        gap = random.uniform(*self.delay) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def get(self, url: str, wait_for: str | None = None, full_scroll=True) -> str:
        err = None
        for attempt in range(1, self.retries + 1):
            self._wait()
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                self._resolve_captcha()
                if wait_for:
                    try:
                        self.page.wait_for_selector(wait_for, timeout=15_000)
                    except Exception:  # noqa: BLE001
                        pass
                self._scroll(full_scroll)
                html = self.page.content()
                if looks_blocked(html):
                    raise BlockedError("captcha page")
                return html
            except Exception as e:  # noqa: BLE001
                err = e
                backoff = 10 * attempt + random.uniform(0, 5)
                log.warning("load failed (%s) attempt %d/%d, retry in %.0fs: %s",
                            e.__class__.__name__, attempt, self.retries, backoff, url)
                time.sleep(backoff)
        raise RuntimeError(f"giving up on {url}: {err}")

    def close(self):
        try:
            if self._cdp:
                self.page.close()  # leave the user's own Chrome running
            else:
                self._ctx.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._pw.stop()


def clean(s):
    if s is None:
        return None
    for ch in ("\u200e", "\u200f", "\u202a", "\u202c"):
        s = s.replace(ch, "")
    s = re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()
    return s or None


def text(el):
    return clean(el.get_text(" ", strip=True)) if el else None


def first_text(soup, *selectors):
    for sel in selectors:
        t = text(soup.select_one(sel))
        if t:
            return t
    return None


def to_number(s):
    if not s:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    return float(m.group().replace(",", "")) if m else None


def to_int(s):
    n = to_number(s)
    return int(n) if n is not None else None


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_date(s):
    if not s:
        return None
    for fmt in ("%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return s


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        key = json.dumps(x, sort_keys=True) if isinstance(x, (dict, list)) else x
        if x and key not in seen:
            seen.add(key)
            out.append(x)
    return out


def decode_after(html: str, pattern: str):
    """Decode the JSON value that follows a regex match (handles nested arrays/objects)."""
    dec = json.JSONDecoder()
    for m in re.finditer(pattern, html):
        try:
            return dec.raw_decode(html, m.end())[0]
        except ValueError:
            continue
    return None


def full_size(url):
    """Amazon image URL without size modifiers, e.g. abc._SS40_.jpg -> abc.jpg"""
    return re.sub(r"\._[^/]*?_?\.(jpe?g|png|webp|gif)$", r".\1", url, flags=re.I) if url else url


def parse_search_page(html: str, domain: str):
    soup = BeautifulSoup(html, "lxml")
    items = []
    for div in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = (div.get("data-asin") or "").strip()
        if not ASIN_RE.match(asin):
            continue
        items.append({
            "asin": asin,
            "sponsored": bool(div.select_one(".puis-sponsored-label-text, .s-sponsored-label-text"))
            or "AdHolder" in (div.get("class") or []),
            "title": text(div.select_one("h2")),
            "card_text": (text(div) or "").lower(),
            "search_price": first_text(div, ".a-price:not(.a-text-price) .a-offscreen"),
            "search_rating": first_text(div, "i.a-icon-star-small .a-icon-alt", "span.a-icon-alt"),
            "search_review_count": first_text(div, "a[href*='customerReviews'] span.a-size-base",
                                              "span.s-underline-text", "a[href*='#customerReviews'] span"),
        })
    nxt = soup.select_one("a.s-pagination-next:not(.s-pagination-disabled)")
    next_url = urljoin(f"https://www.{domain}/", nxt["href"]) if nxt and nxt.get("href") else None
    return items, next_url


SPEC_TABLE_ROWS = [
    "#productDetails_techSpec_section_1 tr", "#productDetails_techSpec_section_2 tr",
    "#technicalSpecifications_section_1 tr", "#productDetails_detailBullets_sections1 tr",
    "#productDetails_expanderTables_depthLeftSections tr", "#productDetails_expanderTables_depthRightSections tr",
    "[id^='productDetails_expanderSectionTables'] tr", "table.prodDetTable tr",
    "#prodDetails table tr", "#tech table tr", "#productOverview_feature_div table tr", "#poExpander table tr",
]


SKIP_SPEC_KEYS = {"customer reviews"}


def extract_raw_specs(soup) -> dict:
    raw: dict[str, str] = {}

    def put(k, v):
        k, v = clean(k), clean(v)
        if k:
            k = k.rstrip(":").strip()
        if k and v and k not in raw and k.lower() not in SKIP_SPEC_KEYS:
            raw[k] = v

    for sel in SPEC_TABLE_ROWS:
        for tr in soup.select(sel):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) >= 2:
                put(text(cells[0]), text(cells[-1]))
    for li in soup.select("#detailBullets_feature_div li span.a-list-item"):
        key_el = li.select_one("span.a-text-bold")
        full = text(li)
        if key_el and full:
            key = text(key_el) or ""
            put(key, full[len(key):])
    for cell in soup.select("#glance_icons_div td, #glance_icons_div .a-column"):
        k = cell.select_one("span.a-text-bold")
        v = cell.select_one("span.handle-overflow:not(.a-text-bold), span.a-size-base:not(.a-text-bold)")
        if k and v:
            put(text(k), text(v))
    return raw


LAPTOP_PROFILE = {
    "processor": ["CPU Model Number", "Processor Name", "CPU Model", "Processor Type", "Processor"],
    "processor_brand": ["Processor Brand", "CPU Manufacturer"],
    "processor_speed": ["Processor Speed", "CPU Speed"],
    "processor_cores": ["Number of Cores", "CPU Cores", "Processor Cores", "Number of Processor Cores"],
    "ram": ["RAM Size", "RAM Memory Installed", "RAM Memory Installed Size", "Installed RAM memory size", "RAM"],
    "ram_type": ["Memory Technology", "Computer Memory Type", "RAM Memory Technology"],
    "max_ram": ["Maximum Memory Supported", "Memory Maximum Supported", "Ram Memory Maximum Size"],
    "storage": ["Hard Drive Size", "Hard Disk Size", "Solid State Drive Capacity", "Flash Memory Size"],
    "storage_type": ["Hard Disk Description", "Hard Drive Interface", "Hard Disk Interface", "Hard Disk Technology"],
    "display": ["Standing screen display size", "Screen Size", "Display Size"],
    "resolution": ["Screen Resolution", "Resolution", "Maximum Display Resolution", "Display Resolution Maximum"],
    "refresh_rate": ["Refresh Rate"],
    "touchscreen": [],
    "graphics": ["Graphics Coprocessor", "Graphics Card Description", "Graphics Chipset Brand"],
    "graphics_memory": ["Graphics Card Ram Size", "Graphics RAM Size", "Graphics Card Ram"],
    "operating_system": ["Operating System", "OS"],
    "battery_life": ["Average Battery Life (in hours)", "Average Battery Life", "Battery Life"],
    "connectivity": ["Network Connectivity Technology", "Connectivity Type", "Connectivity Technology",
                     "Wireless Technology", "Wireless Type"],
    "weight": ["Item Weight", "Weight"],
    "color": ["Colour", "Color"],
}


PROCESSOR_RE = re.compile(
    r"\b(Intel\s+Core\s+(?:Ultra\s+)?(?:i[3579]|[3579])[\s-]*\d{0,5}[A-Z]{0,3}"
    r"|Intel\s+(?:Celeron|Pentium)(?:\s+[A-Z]?\d{3,5}\w*)?"
    r"|AMD\s+Ryzen\s+(?:AI\s+)?\d+(?:\s+\d{3,4}[A-Z]{0,3})?"
    r"|Snapdragon\s+X(?:\s+(?:Elite|Plus))?|Apple\s+M\d(?:\s+(?:Pro|Max))?|MediaTek\s+\w+)", re.I)


GRAPHICS_RE = re.compile(
    r"\b(NVIDIA\s+(?:GeForce\s+)?RTX\s*\d{4}\s*(?:Ti)?(?:\s+\d+\s*GB)?|NVIDIA\s+GeForce\s+\w+\s*\d*"
    r"|AMD\s+Radeon\s+\w+(?:\s+\w+)?|Intel\s+(?:Iris\s+Xe|UHD|Arc)(?:\s+Graphics)?)", re.I)


def norm_size(s):
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB)", s, re.I)
    return f"{float(m.group(1)):g} {m.group(2).upper()}" if m else s


def norm_display(s):
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\"|”|inch|inches|in\b)", s, re.I)
    if m:
        return f"{float(m.group(1)):g} inch"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:cm|centimet)", s, re.I)
    return f"{round(float(m.group(1)) / 2.54, 1):g} inch" if m else s


def storage_kind(*vals):
    blob = " ".join(v for v in vals if v).lower()
    if "ssd" in blob or "solid state" in blob or "nvme" in blob:
        return "SSD"
    if "emmc" in blob:
        return "eMMC"
    if "hdd" in blob or "mechanical" in blob:
        return "HDD"
    return None


def normalize_laptop(raw: dict, title: str, bullets: list) -> dict:
    lower = {k.lower(): v for k, v in raw.items()}
    pick = lambda labels: next((lower[l.lower()] for l in labels if lower.get(l.lower())), None)  # noqa: E731
    n = {key: pick(labels) for key, labels in LAPTOP_PROFILE.items()}
    title = title or ""
    blob = title + " | " + " | ".join(bullets or [])

    proc, pbrand = n["processor"], n["processor_brand"]
    if proc and pbrand and not proc.lower().startswith(pbrand.lower()):
        proc = f"{pbrand} {proc}"
    if not proc or not re.search(r"\d{3,}", proc):  # generic like "Core i5" -> full model from title
        m = PROCESSOR_RE.search(title) or PROCESSOR_RE.search(blob)
        proc = clean(m.group(1)) if m else proc
    n["processor"] = proc

    ram = norm_size(n["ram"])
    if not ram:
        m = re.search(r"(\d{1,3})\s*GB\s*(?:(LPDDR\dX?|DDR\d)\s*)?RAM", blob, re.I) or \
            re.search(r"Memory:\s*(?:\d+x)?(\d{1,3})\s*GB.*?((?:LP)?DDR\dX?)", blob, re.I)
        if m:
            ram = f"{m.group(1)} GB" + (f" {m.group(2).upper()}" if m.lastindex and m.group(2) else "")
    if not n["ram_type"]:
        m = re.search(r"\b((?:LP)?DDR\dX?)\b", blob, re.I)
        n["ram_type"] = m.group(1).upper() if m else None
    if ram and n["ram_type"] and n["ram_type"].lower() not in ram.lower():
        ram = f"{ram} {n['ram_type']}"
    n["ram"] = ram

    kind = storage_kind(n["storage_type"], lower.get("hard disk description"), lower.get("hard drive interface"))
    storage = norm_size(n["storage"])
    if not storage:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB)\s*(SSD|HDD|eMMC)", blob, re.I)
        if m:
            storage = f"{float(m.group(1)):g} {m.group(2).upper()}"
            kind = kind or storage_kind(m.group(3))
    if storage and kind and kind.lower() not in storage.lower():
        storage = f"{storage} {kind}"
    n["storage"], n["storage_type"] = storage, kind or n["storage_type"]

    disp = norm_display(n["display"])
    if not disp:
        m = re.search(r"(\d{2}(?:\.\d{1,2})?)\s*(?:\"|”|-?\s*inch)", blob, re.I)
        disp = f"{float(m.group(1)):g} inch" if m else None
    n["display"] = disp

    if not n["refresh_rate"]:
        m = re.search(r"(\d{2,3})\s*Hz", blob, re.I)
        n["refresh_rate"] = f"{m.group(1)} Hz" if m else None
    if not n["resolution"]:
        m = re.search(r"\b(\d{3,4}\s*x\s*\d{3,4})\b", blob) or \
            re.search(r"\b(FHD\+?|WUXGA|QHD\+?|WQXGA|2\.[58]K|3K|4K|UHD|OLED)\b", blob)
        n["resolution"] = m.group(1).replace(" ", "") if m else None
    if not n["graphics"]:
        m = GRAPHICS_RE.search(blob)
        n["graphics"] = clean(m.group(1)) if m else None
    if not n["operating_system"]:
        m = re.search(r"\b(Windows\s+1[01](?:\s+(?:Home|Pro))?|macOS|Chrome\s*OS|Ubuntu|DOS)\b", blob, re.I)
        n["operating_system"] = m.group(1) if m else None
    if not n["weight"]:
        m = re.search(r"(\d+(?:\.\d+)?)\s*kg\b", blob, re.I)
        n["weight"] = f"{m.group(1)} kg" if m else None

    if not n["processor_cores"]:
        m = re.search(r"\b(\d{1,2})\s*cores?\b", blob, re.I)
        n["processor_cores"] = m.group(1) if m else None
    n["max_ram"] = norm_size(n["max_ram"])
    n["graphics_memory"] = norm_size(n["graphics_memory"])
    touch_blob = " ".join(filter(None, [title, lower.get("special feature"), lower.get("display type"),
                                        lower.get("display technology"), lower.get("human interface input")]))
    n["touchscreen"] = True if re.search(r"touch\s*-?screen|touch display|\btouch\b", touch_blob, re.I) else None
    return n


def price_from(soup, selectors):
    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        t = text(el)
        if not t:
            box = el.find_parent(class_="a-price") or el
            whole, frac = text(box.select_one(".a-price-whole")), text(box.select_one(".a-price-fraction"))
            sym = text(box.select_one(".a-price-symbol")) or ""
            t = f"{sym}{whole.rstrip('.')}.{frac}" if whole and frac else (f"{sym}{whole}" if whole else None)
        if t and to_number(t) is not None:
            return t
    return None


def extract_pricing(soup, default_currency):
    cur = price_from(soup, [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#priceblock_dealprice", "#priceblock_ourprice", "#price_inside_buybox",
    ])
    orig = price_from(soup, [
        "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-text-price[data-a-strike='true'] .a-offscreen",
        "#corePrice_desktop .a-text-price .a-offscreen", "#corePrice_feature_div .a-text-price .a-offscreen",
        "#listPrice", "#priceblock_listprice",
    ])
    if not orig:
        box = text(soup.select_one("#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #apex_desktop")) or ""
        m = re.search(r"(?:M\.R\.P\.?|List Price|Typical price)\s*:?\s*([₹$£€]\s?[\d,]+(?:\.\d+)?)", box, re.I)
        orig = m.group(1) if m else None
    cp, op = to_number(cur), to_number(orig)
    disc_text = first_text(soup, "#corePriceDisplay_desktop_feature_div .savingsPercentage", ".savingsPercentage")
    disc = to_number(disc_text)
    if disc is None and cp and op and op > cp:
        disc = round((op - cp) / op * 100, 1)
    currency = next((c for s, c in CURRENCY_SYMBOLS.items() if cur and s in cur), default_currency)
    return {
        "current_price": cp, "original_price": op,
        "discount_percentage": abs(disc) if disc is not None else None,
        "savings_amount": round(op - cp, 2) if cp and op and op > cp else None,
        "currency": currency, "current_price_display": cur, "original_price_display": orig,
        "discount": disc_text or (f"-{disc:g}%" if disc else None),
    }


def extract_seller(soup):
    sold_by = first_text(soup, "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
                         "#sellerProfileTriggerId",
                         "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Sold by']")
    ships = first_text(soup, "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message",
                       "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Ships from']")
    merchant = text(soup.select_one("#merchant-info")) or ""
    if not ships and re.search(r"fulfilled by amazon|ships from and sold by amazon", merchant, re.I):
        ships = "Amazon"
    if not sold_by and re.search(r"sold by amazon", merchant, re.I):
        sold_by = "Amazon"
    seller_id = None
    for a in soup.select("#sellerProfileTriggerId, #merchantInfoFeature_feature_div a, #merchant-info a"):
        m = re.search(r"[?&](?:seller|smid)=([A-Z0-9]+)", a.get("href") or "")
        if m:
            seller_id = m.group(1)
            break
    if not seller_id:
        inp = soup.select_one("input#merchantID, input[name='merchantID']")
        seller_id = inp.get("value") if inp and inp.get("value") else None
    if ships:
        fulfillment = "Fulfilled by Amazon" if "amazon" in ships.lower() else "Fulfilled by seller"
    else:
        fulfillment = None
    return {"seller_name": sold_by, "seller_id": seller_id, "ships_from": ships, "fulfillment_type": fulfillment}


def extract_offers(soup):
    offers = []
    for card in soup.select("#vsxoffers_feature_div .a-carousel-card, div.offers-items, div[id^='itembox-']"):
        t = first_text(card, ".offers-items-title", "h6", ".a-text-bold")
        d = first_text(card, ".a-truncate-full", ".offers-items-content", "span.a-size-base")
        if t and d:
            offers.append({"offer_type": t, "offer_details": d})
    return dedupe(offers)


def extract_badges(soup):
    """Service icons under the price: replacement, warranty, pay on delivery, free delivery..."""
    out = []
    for sel in ("#iconfarmv2_feature_div .icon-content", "#iconfarmv2_feature_div a.a-size-small",
                "#iconfarmv2_feature_div span.a-size-small", "#icon-farm-container .a-size-small",
                "#productSupportAndReturnPolicy_feature_div .a-size-small",
                "#productSupportAndReturnPolicy_feature_div a", "#RETURNS_POLICY span"):
        out += [text(e) for e in soup.select(sel)]
    return dedupe([b for b in out if b and len(b) < 120])


def extract_shipping(soup):
    block = soup.select_one("#mir-layout-DELIVERY_BLOCK") or soup.select_one("#deliveryBlockMessage")
    timed = soup.select_one("[data-csa-c-delivery-time]")
    est = (timed.get("data-csa-c-delivery-time") if timed else None) or \
        text(soup.select_one("#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE .a-text-bold"))
    cost = None
    priced = soup.select_one("[data-csa-c-delivery-price]")
    if priced:
        p = priced.get("data-csa-c-delivery-price") or ""
        cost = 0.0 if "free" in p.lower() else to_number(p)
    parts = [text(soup.select_one(f"#mir-layout-DELIVERY_BLOCK-slot-{slot}"))
             for slot in ("PRIMARY_DELIVERY_MESSAGE_LARGE", "SECONDARY_DELIVERY_MESSAGE_LARGE")]
    parts = [p for p in parts if p] or [text(block)]
    info = " | ".join(clean(re.sub(r"\s+([.,])", r"\1", re.split(r"\bDetails\b", p)[0])) or "" for p in parts if p)
    info = info.strip(" |")[:300] or None
    if cost is None and info and info.upper().startswith("FREE"):
        cost = 0.0
    return {"delivery_information": info, "estimated_delivery": clean(est), "shipping_cost": cost}


def extract_media(soup, html):
    images, thumbs = [], []
    initial = decode_after(html, r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*")
    if isinstance(initial, list):
        for img in initial:
            if isinstance(img, dict):
                images.append(img.get("hiRes") or img.get("large"))
                thumbs.append(img.get("thumb"))
    if not images:
        for i in soup.select("#altImages img"):
            src = i.get("src") or ""
            if src and "play-icon" not in src and "PKdp-play" not in src:
                thumbs.append(src)
                images.append(full_size(src))
        land = soup.select_one("#landingImage, #imgBlkFront")
        if land and land.get("data-old-hires"):
            images.insert(0, land["data-old-hires"])

    videos, vthumbs, vtitles = [], [], []
    for m in re.finditer(r'"videos"\s*:\s*', html):
        try:
            arr = json.JSONDecoder().raw_decode(html, m.end())[0]
        except ValueError:
            continue
        if not isinstance(arr, list):
            continue
        for v in arr:
            if isinstance(v, dict) and str(v.get("url", "")).startswith("http"):
                videos.append(v["url"])
                vthumbs.append(v.get("slateUrl") or v.get("thumbUrl") or v.get("thumb"))
                vtitles.append(clean(v.get("title")))
    if not videos:
        for s in soup.find_all("script"):
            body = s.string or ""
            if '"videos"' in body or "videoUrl" in body:
                videos += re.findall(r"https://[^\"'\s\\]+?\.(?:mp4|m3u8)", body)
    return {
        "images": {"original_urls": dedupe(images), "thumbnail_urls": dedupe(thumbs)},
        "videos": {"original_urls": dedupe(videos), "thumbnail_urls": dedupe(vthumbs),
                   "titles": dedupe(vtitles)},
    }


def extract_variants(soup, html, asin, domain):
    dom = {}
    for li in soup.select("#twister li[data-defaultasin], #twister li[data-asin], "
                          "[id^='inline-twister'] li[data-asin], [id^='inline-twister'] li[data-csa-c-item-id]"):
        a = li.get("data-defaultasin") or li.get("data-asin") or (li.get("data-csa-c-item-id") or "").split(":")[-1]
        if not ASIN_RE.match(a or ""):
            continue
        cls = " ".join(li.get("class") or []) + " " + " ".join(
            c for b in li.select(".a-button, .swatch-title-text-container") for c in (b.get("class") or []))
        label = li.get("title") and re.sub(r"^Click to select\s*", "", li["title"])
        if not label:
            label = first_text(li, ".swatch-title-text-display", ".swatch-title-text", ".a-button-text img[alt]")
        dom[a] = {
            "available": not re.search(r"unavailable", cls, re.I),
            "price": first_text(li, ".twisterSwatchPrice", ".a-price .a-offscreen", ".apex-pricetopay-value"),
            "label": clean(label),
        }

    variants = []
    values = decode_after(html, r'"dimensionValuesDisplayData"\s*:\s*')
    dims = decode_after(html, r'"dimensionsDisplay"\s*:\s*') or \
        [re.sub(r"_name$", "", d).replace("_", " ").title()
         for d in (decode_after(html, r'"dimensions"\s*:\s*') or []) if isinstance(d, str)]
    if isinstance(values, dict) and values:
        for a, vals in values.items():
            vals = vals if isinstance(vals, list) else [str(vals)]
            info = dom.get(a, {})
            variants.append({
                "variant_type": " / ".join(dims) if dims else "Variant",
                "variant_name": " / ".join(vals),
                "attributes": dict(zip(dims, vals)) if dims and len(dims) == len(vals) else {},
                "asin": a, "price": to_number(info.get("price")), "price_display": info.get("price"),
                "is_selected": a == asin, "is_available": info.get("available", True),
                "url": f"https://www.{domain}/dp/{a}",
            })
    else:
        for a, info in dom.items():
            if info.get("label"):
                variants.append({
                    "variant_type": "Variant", "variant_name": info["label"], "attributes": {},
                    "asin": a, "price": to_number(info["price"]), "price_display": info["price"],
                    "is_selected": a == asin, "is_available": info["available"],
                    "url": f"https://www.{domain}/dp/{a}",
                })
    return variants


REVIEW_TITLE_SEL = ["[data-hook='review-title']", "[data-hook='reviewTitle']", ".review-title-content",
                    ".review-title", "a.review-title", "span.review-title"]
REVIEW_BODY_SEL = ["[data-hook='review-body']", "[data-hook='review-collapsed']", "[data-hook='reviewText']",
                   ".review-text-content", ".review-text", ".reviewText"]


def _review_part(r, selectors):
    """Text of the first matching element, preferring the original (untranslated) text."""
    for sel in selectors:
        el = r.select_one(sel)
        if not el:
            continue
        orig = el.select_one(".cr-original-review-content")
        el = orig or el
        for junk in el.select("i, .a-icon-alt, .a-letter-space, script, style, .cr-translated-review-content"):
            junk.decompose()
        t = text(el)
        if t:
            t = re.sub(r"^\d(?:\.\d)? out of 5 stars\s*", "", t)
            t = re.sub(r"\s*(Read more|See more|Read less|The media could not be loaded\.?)\s*", " ", t)
            t = clean(t)
            if t:
                return t
    return None


def parse_reviews(soup):
    blocks = soup.select("[data-hook='review']")
    if not blocks:
        blocks = soup.select("[id^='customer_review-'], [id^='customer_review_foreign-']")
    reviews = []
    for r in blocks:
        rid = r.get("id") or ""
        if not re.match(r"^R[A-Z0-9]{8,}$", rid.replace("customer_review-", "").replace("customer_review_foreign-", "")):
            inner = r.select_one("[id^='customer_review']")
            rid = inner.get("id") if inner else rid
        rid = rid.replace("customer_review-", "").replace("customer_review_foreign-", "") or None
        rating = to_number(first_text(r, "[data-hook='review-star-rating'] .a-icon-alt",
                                      "[data-hook='cmps-review-star-rating'] .a-icon-alt",
                                      "i.review-rating .a-icon-alt", "i[class*='a-star'] .a-icon-alt"))
        country = date = None
        dm = re.search(r"Reviewed in (.+?) on (.+)$", text(r.select_one("[data-hook='review-date']")) or "")
        if dm:
            country, date = re.sub(r"^the\s+", "", dm.group(1)), iso_date(dm.group(2))
        helpful_txt = text(r.select_one("[data-hook='helpful-vote-statement']"))
        helpful = (1 if helpful_txt.lower().startswith("one") else to_int(helpful_txt)) if helpful_txt else None
        whole = text(r) or ""
        reviews.append({
            "review_id": rid,
            "reviewer_name": first_text(r, ".a-profile-name", "[data-hook='genome-widget'] .a-profile-content"),
            "review_title": _review_part(r, REVIEW_TITLE_SEL),
            "review_comment": _review_part(r, REVIEW_BODY_SEL),
            "rating": rating, "review_date": date, "review_country": country,
            "verified_purchase": bool(r.select_one("[data-hook='avp-badge'], [data-hook='avp-badge-linkless']"))
            or "Verified Purchase" in whole,
            "helpful_votes": helpful,
            "reviewed_variant": text(r.select_one("[data-hook='format-strip']")),
            "review_images": dedupe([full_size(i.get("src")) for i in
                                     r.select("img[data-hook='review-image-tile'], img.review-image-tile")]),
            "review_videos": dedupe([v.get("data-video-url") for v in r.select("[data-video-url]")]),
        })
    return reviews


def merge_reviews(*lists):
    """Same review can appear on the product page (often cut short) and on the review pages
    (complete). Keep one copy per review and fill its empty fields from the other copy."""
    merged, order = {}, []
    for lst in lists:
        for r in lst:
            key = r["review_id"] or (r["reviewer_name"], r["review_date"], r["rating"])
            if key not in merged:
                merged[key] = dict(r)
                order.append(key)
                continue
            cur = merged[key]
            for f, v in r.items():
                if v in (None, [], "") or (f == "verified_purchase" and not v):
                    continue
                if cur.get(f) in (None, [], "") or (f == "verified_purchase" and not cur.get(f)) or \
                        (f == "review_comment" and len(v) > len(cur.get(f) or "")):
                    cur[f] = v
    return [merged[k] for k in order]


def extract_rating_summary(soup, html):
    pop = soup.select_one("#acrPopover")
    overall = to_number(pop.get("title")) if pop and pop.get("title") else \
        to_number(first_text(soup, "[data-hook='rating-out-of-text']", "#acrPopover .a-icon-alt"))
    total_ratings = to_int(first_text(soup, "#acrCustomerReviewText", "[data-hook='total-review-count']"))
    m = re.search(r"([\d,]+)\s+with reviews", html)
    total_reviews = to_int(m.group(1)) if m else None
    dist = {}
    for a in soup.select("#histogramTable [aria-label], .histogram-row-container[aria-label]"):
        mm = re.search(r"(\d+)\s*percent of reviews have (\d) stars?", a.get("aria-label", ""), re.I)
        if mm:
            dist[f"{mm.group(2)}_star"] = int(mm.group(1))
    if not dist:
        for tr in soup.select("#histogramTable tr, #histogramTable li"):
            t = text(tr) or ""
            star, pct = re.search(r"(\d)\s*star", t, re.I), re.search(r"(\d+)%", t)
            if star and pct:
                dist[f"{star.group(1)}_star"] = int(pct.group(1))
    return overall, total_ratings, total_reviews, dict(sorted(dist.items(), reverse=True))


def collect_review_pages(browser, asin, domain, max_pages):
    """Returns (reviews, pages_checked, total_reviews, login_required)."""
    reviews, total, pages = [], None, 0
    for n in range(1, max_pages + 1):
        url = (f"https://www.{domain}/product-reviews/{asin}/?reviewerType=all_reviews"
               f"&sortBy=recent&pageNumber={n}")
        html = browser.get(url, wait_for="[data-hook='review'], #ap_email, #cm_cr-review_list", full_scroll=False)
        if "/ap/signin" in browser.url or "id=\"ap_email\"" in html:
            return reviews, pages, total, True
        pages += 1
        soup = BeautifulSoup(html, "lxml")
        if total is None:
            info = text(soup.select_one("[data-hook='cr-filter-info-review-rating-count']")) or ""
            m = re.search(r"([\d,]+)\s+with reviews", info) or re.search(r"([\d,]+)\s+(?:customer|global)\s+reviews?", info)
            total = to_int(m.group(1)) if m else None
        page_reviews = parse_reviews(soup)
        reviews += page_reviews
        if not page_reviews or not soup.select_one("li.a-last:not(.a-disabled) a"):
            break
    return reviews, pages, total, False


# ======================================================================================
# Air conditioner and refrigerator profiles
# ======================================================================================
def _picker(raw):
    lower = {k.lower(): v for k, v in raw.items()}
    return lambda labels: next((lower[l.lower()] for l in labels if lower.get(l.lower())), None)


def _first(pattern, *texts, flags=re.I):
    for t in texts:
        m = re.search(pattern, t or "", flags)
        if m:
            return m
    return None


def _star(*texts):
    m = _first(r"(\d)\s*-?\s*Star", *texts)
    return f"{m.group(1)} Star" if m else None


AC_PROFILE = {
    "brand": ["Brand"],
    "model_number": ["Item model number", "Model Number", "Model Name"],
    "tonnage": ["Capacity", "Cooling Capacity (Tons)", "Tonnage"],
    "ac_type": ["Installation Type", "Form Factor", "Type"],
    "energy_rating": ["Energy Efficiency", "Energy Rating", "Energy Star Rating", "Star Rating", "BEE Star Rating"],
    "inverter": [],
    "cooling_capacity": ["Cooling Power", "Cooling Capacity", "Cooling Power (Watts)"],
    "iseer": ["ISEER", "ISEER Value", "Energy Efficiency Ratio"],
    "annual_energy_consumption": ["Annual Energy Consumption"],
    "condenser_coil": ["Condenser Coil", "Coil Material", "Condenser Coil Material", "Material"],
    "refrigerant": ["Refrigerant"],
    "noise_level": ["Noise Level", "Noise"],
    "special_features": ["Special Feature", "Special Features"],
    "power_consumption": ["Wattage", "Power Consumption"],
    "voltage": ["Voltage"],
    "color": ["Colour", "Color"],
}


def normalize_ac(raw, title, bullets):
    pick = _picker(raw)
    n = {k: pick(v) for k, v in AC_PROFILE.items()}
    blob = " | ".join([title or ""] + list(bullets or []))
    cap = n["tonnage"] if n["tonnage"] and "litre" not in n["tonnage"].lower() else None
    m = _first(r"(\d(?:\.\d{1,2})?)\s*-?\s*Tons?\b", cap, title, blob)
    n["tonnage"] = float(m.group(1)) if m else None
    m = _first(r"\b(split|window|portable|cassette|tower|floor standing)\b", n["ac_type"], title)
    n["ac_type"] = m.group(1).title() if m else n["ac_type"]
    n["energy_rating"] = _star(n["energy_rating"], title, blob) or n["energy_rating"]
    inv_blob = " ".join(filter(None, [title, pick(["Compressor Type", "Compressor"]), n["special_features"]]))
    n["inverter"] = False if re.search(r"non[\s-]*inverter|fixed[\s-]*speed", inv_blob, re.I) else \
        (True if re.search(r"inverter", inv_blob, re.I) else None)
    m = _first(r"(\d{3,5})\s*(?:W\b|Watts?)", n["cooling_capacity"], blob)
    n["cooling_capacity"] = f"{m.group(1)} W" if m else n["cooling_capacity"]
    m = _first(r"\b(copper|aluminium|aluminum|alloy)\b", n["condenser_coil"], title, blob)
    n["condenser_coil"] = m.group(1).title() if m else n["condenser_coil"]
    m = _first(r"(?:ISEER|EER)\s*(?:value|rating)?\s*(?:of|:|-)?\s*(\d(?:\.\d{1,2})?)", n["iseer"], blob)
    n["iseer"] = float(m.group(1)) if m else n["iseer"]
    if not n["refrigerant"]:
        m = _first(r"\b(R-?32|R-?410A|R-?22|R-?290)\b", blob)
        n["refrigerant"] = m.group(1).upper() if m else None
    return n


FRIDGE_PROFILE = {
    "brand": ["Brand"],
    "model_number": ["Item model number", "Model Number", "Model Name"],
    "capacity_litres": ["Capacity", "Total Capacity", "Gross Capacity", "Net Capacity"],
    "door_type": ["Door Style", "Configuration", "Door Type", "Number of Doors"],
    "energy_rating": ["Energy Efficiency", "Energy Rating", "Energy Star Rating", "Star Rating", "BEE Star Rating"],
    "compressor": ["Compressor Type", "Compressor"],
    "defrost_type": ["Defrost System", "Defrost Type"],
    "freezer_capacity_litres": ["Freezer Capacity", "Freezer Capacity (Litres)"],
    "annual_energy_consumption": ["Annual Energy Consumption"],
    "convertible": [],
    "shelf_material": ["Shelf Type", "Shelf Material"],
    "special_features": ["Special Feature", "Special Features"],
    "color": ["Colour", "Color"],
}
DOOR_RE = r"\b(single[\s-]*door|double[\s-]*door|triple[\s-]*door|side[\s-]*by[\s-]*side|french[\s-]*door|" \
          r"multi[\s-]*door|four[\s-]*door|bottom[\s-]*(?:mount|freezer)|top[\s-]*(?:mount|freezer)|mini)\b"


def normalize_fridge(raw, title, bullets):
    pick = _picker(raw)
    n = {k: pick(v) for k, v in FRIDGE_PROFILE.items()}
    blob = " | ".join([title or ""] + list(bullets or []))
    m = _first(r"(\d{2,4})\s*(?:L\b|Litres?|Liters?|ltrs?)", n["capacity_litres"], title, blob)
    n["capacity_litres"] = int(m.group(1)) if m else None
    m = _first(r"(\d{2,4})\s*(?:L\b|Litres?|Liters?)", n["freezer_capacity_litres"])
    n["freezer_capacity_litres"] = int(m.group(1)) if m else None
    m = _first(DOOR_RE, n["door_type"], title)
    n["door_type"] = re.sub(r"[\s-]+", " ", m.group(1)).title().replace("By", "by") if m else n["door_type"]
    n["energy_rating"] = _star(n["energy_rating"], title, blob) or n["energy_rating"]
    comp = " ".join(filter(None, [n["compressor"], title]))
    if re.search(r"non[\s-]*inverter", comp, re.I):
        n["compressor"] = "Non-Inverter"
    elif re.search(r"inverter", comp, re.I):
        n["compressor"] = "Inverter"
    elif re.search(r"reciproca|conventional", comp, re.I):
        n["compressor"] = "Reciprocating"
    m = _first(r"\b(frost[\s-]*free|direct[\s-]*cool|no[\s-]*frost)\b", n["defrost_type"], title, blob)
    n["defrost_type"] = re.sub(r"[\s-]+", " ", m.group(1)).title() if m else n["defrost_type"]
    n["convertible"] = True if re.search(r"convertible", blob, re.I) else None
    m = _first(r"\b(toughened glass|tempered glass|wire)\b", n["shelf_material"], blob)
    n["shelf_material"] = m.group(1).title() if m else n["shelf_material"]
    return n


# ======================================================================================
# The three categories
# ======================================================================================
CATEGORIES = {
    "hp_laptops": {
        "label": "HP Laptops", "profile": "laptop", "subcategory": "Laptops",
        "default_category": "Computers & Accessories", "normalize": normalize_laptop,
        "search": {
            "amazon.in": "https://www.amazon.in/s?k=hp+laptop&i=computers&rh=n%3A1375424031%2Cp_89%3AHP",
            "amazon.com": "https://www.amazon.com/s?k=hp+laptop&i=computers&rh=n%3A565108%2Cp_89%3AHP",
        },
        "brand": r"^(hp|hewlett[\s-]*packard)\b",
        "card_must": r"\bhp\b", "title_must": r"\b(laptop|notebook|chromebook)\b",
        "crumb_must": r"laptop|notebook|chromebook",
        "strong": r"\b(core\s*(?:ultra|i\d)|ryzen|\d+\s*gb\s*(?:ram|ddr|lpddr)|\d+\s*(?:gb|tb)\s*ssd)",
        "exclude": r"\b(bag|backpack|sleeve|skin|charger|adapter only|battery for|screen protector|keyboard cover)\b",
    },
    "air_conditioners": {
        "label": "Air Conditioners", "profile": "air_conditioner", "subcategory": "Air Conditioners",
        "default_category": "Home & Kitchen", "normalize": normalize_ac,
        "search": {"amazon.in": "https://www.amazon.in/s?k=air+conditioner",
                   "amazon.com": "https://www.amazon.com/s?k=air+conditioner"},
        "brand": None,
        "card_must": r"\b(ac|air conditioner)\b", "title_must": r"\b(ac|air conditioner)\b",
        "crumb_must": r"air conditioner",
        "strong": r"\d(?:\.\d{1,2})?\s*-?\s*tons?\b",
        "exclude": r"\b(cover|stand|remote|bracket|pipe|cleaner|cleaning|spray|filter only|"
                   r"outdoor unit stand|curtain|fan|cooler|tape|insulation|copper pipe|drain)\b",
    },
    "refrigerators": {
        "label": "Refrigerators", "profile": "refrigerator", "subcategory": "Refrigerators",
        "default_category": "Home & Kitchen", "normalize": normalize_fridge,
        "search": {"amazon.in": "https://www.amazon.in/s?k=refrigerator",
                   "amazon.com": "https://www.amazon.com/s?k=refrigerator"},
        "brand": None,
        "card_must": r"\b(refrigerator|fridge)\b", "title_must": r"\b(refrigerator|fridge)\b",
        "crumb_must": r"refrigerator",
        "strong": r"\b\d{2,4}\s*(?:l|litres?|liters?|ltrs?)\b",
        "exclude": r"\b(cover|mat|stand|stabili[sz]er|organi[sz]er|deodori[sz]er|tray|egg|magnet|toy|"
                   r"bottle|container|gasket|thermostat|handle|basket|water filter|lock)\b",
    },
}


def search_url(cat_key, domain):
    cat = CATEGORIES[cat_key]
    k = {"hp_laptops": "hp+laptop", "air_conditioners": "air+conditioner", "refrigerators": "refrigerator"}[cat_key]
    return cat["search"].get(domain, f"https://www.{domain}/s?k={k}")


def card_matches(item, cat):
    title = (item.get("title") or item.get("card_text") or "").lower()
    if not re.search(cat["card_must"], item.get("card_text") or title, re.I):
        return False
    if re.search(cat["strong"], title, re.I):  # clearly the real product (has tonnage/litres/specs)
        return True
    return not re.search(cat["exclude"], title, re.I)


def product_matches(rec, crumbs, cat):
    d = rec["product_details"]
    title = d["title"]
    if cat["brand"] and not re.search(cat["brand"], (d.get("brand") or title).strip(), re.I) \
            and not re.search(cat["brand"], title.strip(), re.I):
        return False, f"brand is {d.get('brand')!r}"
    if crumbs:
        if not any(re.search(cat["crumb_must"], c, re.I) for c in crumbs):
            return False, f"category is {' > '.join(crumbs)!r}"
    elif not re.search(cat["title_must"], title, re.I) or \
            (re.search(cat["exclude"], title, re.I) and not re.search(cat["strong"], title, re.I)):
        return False, "title does not look like this category"
    return True, ""


# ======================================================================================
# Build one record in the provided schema
# ======================================================================================
def build_record(html, asin, domain, cat, sponsored, review_info):
    soup = BeautifulSoup(html, "lxml")
    mp = MARKETPLACES.get(domain, {})
    errors: list[str] = []
    title = text(soup.select_one("#productTitle"))
    if not title:
        raise ValueError("no #productTitle on page (not a product page or layout changed)")

    raw = extract_raw_specs(soup)
    lower = {k.lower(): v for k, v in raw.items()}
    rawget = lambda *keys: next((lower[k.lower()] for k in keys if lower.get(k.lower())), None)  # noqa: E731

    bullets = [t for t in (text(li) for li in soup.select("#feature-bullets ul li span.a-list-item"))
               if t and not re.match(r"(see more product details|from the manufacturer)", t, re.I)]
    def _desc(t):
        if not t:
            return None
        t = re.sub(r"\b(Previous page|Next page|From the manufacturer|Product description|Read more)\b", " ", t, flags=re.I)
        t = clean(t)
        return t[:5000] if t and len(t) >= 30 else None  # carousel buttons alone are not a description
    description = _desc(text(soup.select_one("#productDescription"))) or \
        _desc(text(soup.select_one("#aplus_feature_div, #aplus")))
    crumbs = [text(a) for a in soup.select("#wayfinding-breadcrumbs_feature_div li a") if text(a)]

    brand = rawget("Brand")
    if not brand:
        byline = text(soup.select_one("#bylineInfo"))
        m = re.match(r"^(?:Visit the|Brand:)\s*(.+?)(?:\s+Store)?$", byline or "", re.I)
        brand = clean(m.group(1)) if m else byline

    p2 = extract_pricing(soup, mp.get("currency", "USD"))
    seller = extract_seller(soup)
    badges = extract_badges(soup)
    shipping = extract_shipping(soup)
    media = extract_media(soup, html)
    overall, total_ratings, total_reviews, dist = extract_rating_summary(soup, html)

    reviews = merge_reviews(review_info["reviews"], parse_reviews(soup))
    for r in reviews:
        r.pop("reviewed_variant", None)  # keep exactly the schema's review fields

    variants = [{
        "variant_type": v["variant_type"], "variant_name": v["variant_name"], "asin": v["asin"],
        "price": v["price_display"], "is_selected": v["is_selected"], "is_available": v["is_available"],
        "url": v["url"],
    } for v in extract_variants(soup, html, asin, domain)]
    for v in variants:  # the selected variant is this page: use its price
        if v["is_selected"] and not v["price"]:
            v["price"] = p2["current_price_display"]

    warranty = rawget("Warranty", "Warranty Description", "Manufacturer Warranty", "Warranty Type") or \
        next((b for b in badges if "warranty" in b.lower()), None) or \
        next((b for b in bullets if "warranty" in b.lower()), None)
    condition = "Renewed" if re.search(r"\b(renewed|refurbished)\b", title, re.I) else \
        ("New" if p2["current_price"] is not None else None)

    record = {
        "asin": asin,
        "schema_version": 1,
        "source": {"platform": "Amazon", "marketplace": mp.get("marketplace", f"Amazon ({domain})"),
                   "country": mp.get("country", "Unknown"), "domain": domain,
                   "product_url": f"https://www.{domain}/dp/{asin}"},
        "product_details": {
            "title": title, "brand": brand,
            "manufacturer": ", ".join(dedupe([x.strip() for x in (rawget("Manufacturer") or "").split(",")])) or None,
            "model": rawget("Item model number", "Model Number", "Model Name"),
            "category": crumbs[0] if crumbs else cat["default_category"],
            "subcategory": cat["subcategory"],
            "description": description, "feature_bullets": bullets,
            "condition": condition, "availability": text(soup.select_one("#availability")),
            "dimensions": rawget("Product Dimensions", "Item Dimensions LxWxH", "Item dimensions L x W x H",
                                 "Item Dimensions L x W x Thickness", "Item Dimensions") or
            next((v for k, v in raw.items() if "dimension" in k.lower() and "package" not in k.lower()), None),
            "weight": rawget("Item Weight", "Weight"), "warranty": warranty,
            "country_of_origin": rawget("Country of Origin"),
            "pricing": {"currency": p2["currency"], "current_price": p2["current_price"],
                        "original_price": p2["original_price"], "discount_percentage": p2["discount_percentage"],
                        "price_display": p2["current_price_display"],
                        "original_price_display": p2["original_price_display"]},
            "seller": {"seller_name": seller["seller_name"], "fulfilled_by": seller["ships_from"]},
            "offers": extract_offers(soup),
            "shipping": {"delivery_information": shipping["delivery_information"],
                         "shipping_cost": shipping["shipping_cost"],
                         "estimated_delivery": shipping["estimated_delivery"]},
        },
        "product_variants": variants,
        "product_media": {"images": media["images"]["original_urls"],
                          "thumbnails": media["images"]["thumbnail_urls"],
                          "videos": media["videos"]["original_urls"]},
        "customer_feedback": {
            "overall_rating": overall, "total_ratings": total_ratings,
            "total_reviews": review_info["total_reviews"] or total_reviews, "rating_scale": 5,
            "rating_distribution": {k: f"{v}%" for k, v in dist.items()},
            "reviews": reviews,
        },
        "product_specifications": {"category_profile": cat["profile"],
                                   "normalized": cat["normalize"](raw, title, bullets), "raw": raw},
        "platform_specific": {"is_sponsored_listing": bool(sponsored)},
        "scraping_metadata": {
            "scraped_at": now_iso(), "last_updated": now_iso(), "scraping_status": "success",
            "data_summary": {"images_scraped": len(media["images"]["original_urls"]),
                             "videos_scraped": len(media["videos"]["original_urls"]),
                             "reviews_scraped": len(reviews), "variants_scraped": len(variants),
                             "specifications_scraped": len(raw)},
            "errors": errors,
        },
    }
    for label, value in (("price", p2["current_price"]), ("specifications", raw),
                         ("rating", overall), ("images", media["images"]["original_urls"])):
        if not value:
            errors.append(f"missing {label}")
    if review_info["login_required"]:
        errors.append("review pages asked for sign-in; only product-page reviews collected")
    if errors:
        record["scraping_metadata"]["scraping_status"] = "partial"
    return record, crumbs


# ======================================================================================
# The provided schema (built in) and validation
# ======================================================================================
PRODUCT_SCHEMA = json.loads(r'''
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Universal Amazon Product Record",
  "description": "One schema for every product category (laptops, refrigerators, air conditioners, mobiles, ...). Category-specific data lives only in product_specifications.normalized, whose keys are defined by a category profile. Everything Amazon lists is kept in product_specifications.raw.",
  "type": "object",
  "required": [
    "asin",
    "schema_version",
    "source",
    "product_details",
    "product_specifications",
    "scraping_metadata"
  ],
  "properties": {
    "asin": {
      "type": "string",
      "pattern": "^[A-Z0-9]{10}$"
    },
    "schema_version": {
      "type": "integer",
      "const": 1
    },
    "source": {
      "type": "object",
      "required": [
        "platform",
        "marketplace",
        "country",
        "domain",
        "product_url"
      ],
      "properties": {
        "platform": {
          "type": "string"
        },
        "marketplace": {
          "type": "string"
        },
        "country": {
          "type": "string"
        },
        "domain": {
          "type": "string"
        },
        "product_url": {
          "type": "string",
          "format": "uri"
        }
      }
    },
    "product_details": {
      "type": "object",
      "required": [
        "title",
        "category"
      ],
      "properties": {
        "title": {
          "type": "string"
        },
        "brand": {
          "type": [
            "string",
            "null"
          ]
        },
        "manufacturer": {
          "type": [
            "string",
            "null"
          ]
        },
        "model": {
          "type": [
            "string",
            "null"
          ]
        },
        "category": {
          "type": "string",
          "description": "Top level, e.g. Electronics, Home & Kitchen"
        },
        "subcategory": {
          "type": "string",
          "description": "e.g. Laptops, Refrigerators, Air Conditioners, Smartphones"
        },
        "description": {
          "type": [
            "string",
            "null"
          ]
        },
        "feature_bullets": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "condition": {
          "type": [
            "string",
            "null"
          ]
        },
        "availability": {
          "type": [
            "string",
            "null"
          ]
        },
        "dimensions": {
          "type": [
            "string",
            "null"
          ]
        },
        "weight": {
          "type": [
            "string",
            "null"
          ]
        },
        "warranty": {
          "type": [
            "string",
            "null"
          ]
        },
        "country_of_origin": {
          "type": [
            "string",
            "null"
          ]
        },
        "pricing": {
          "type": "object",
          "properties": {
            "currency": {
              "type": "string"
            },
            "current_price": {
              "type": [
                "number",
                "null"
              ]
            },
            "original_price": {
              "type": [
                "number",
                "null"
              ]
            },
            "discount_percentage": {
              "type": [
                "number",
                "null"
              ]
            },
            "price_display": {
              "type": [
                "string",
                "null"
              ]
            },
            "original_price_display": {
              "type": [
                "string",
                "null"
              ]
            }
          }
        },
        "seller": {
          "type": "object",
          "properties": {
            "seller_name": {
              "type": [
                "string",
                "null"
              ]
            },
            "fulfilled_by": {
              "type": [
                "string",
                "null"
              ]
            }
          }
        },
        "offers": {
          "type": "array",
          "items": {
            "type": "object",
            "required": [
              "offer_type",
              "offer_details"
            ],
            "properties": {
              "offer_type": {
                "type": "string"
              },
              "offer_details": {
                "type": "string"
              }
            }
          }
        },
        "shipping": {
          "type": "object",
          "properties": {
            "delivery_information": {
              "type": [
                "string",
                "null"
              ]
            },
            "shipping_cost": {
              "type": [
                "number",
                "null"
              ]
            },
            "estimated_delivery": {
              "type": [
                "string",
                "null"
              ]
            }
          }
        }
      }
    },
    "product_variants": {
      "type": "array",
      "description": "variant_type is free text from the page (color, size, style, capacity, configuration, pattern, ...), so it works for every category.",
      "items": {
        "type": "object",
        "required": [
          "variant_type",
          "variant_name"
        ],
        "properties": {
          "variant_type": {
            "type": "string"
          },
          "variant_name": {
            "type": "string"
          },
          "asin": {
            "type": [
              "string",
              "null"
            ]
          },
          "price": {
            "type": [
              "string",
              "null"
            ]
          },
          "is_selected": {
            "type": "boolean"
          },
          "is_available": {
            "type": "boolean"
          },
          "url": {
            "type": [
              "string",
              "null"
            ]
          }
        }
      }
    },
    "product_media": {
      "type": "object",
      "properties": {
        "images": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "thumbnails": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "videos": {
          "type": "array",
          "items": {
            "type": "string"
          }
        }
      }
    },
    "customer_feedback": {
      "type": "object",
      "properties": {
        "overall_rating": {
          "type": [
            "number",
            "null"
          ]
        },
        "total_ratings": {
          "type": [
            "integer",
            "null"
          ]
        },
        "total_reviews": {
          "type": [
            "integer",
            "null"
          ]
        },
        "rating_scale": {
          "type": "integer"
        },
        "rating_distribution": {
          "type": "object",
          "additionalProperties": {
            "type": "string"
          }
        },
        "reviews": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "review_id": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "reviewer_name": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "review_title": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "review_comment": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "rating": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "review_date": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "review_country": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "verified_purchase": {
                "type": "boolean"
              },
              "helpful_votes": {
                "type": [
                  "integer",
                  "null"
                ]
              },
              "review_images": {
                "type": "array",
                "items": {
                  "type": "string"
                }
              },
              "review_videos": {
                "type": "array",
                "items": {
                  "type": "string"
                }
              }
            }
          }
        }
      }
    },
    "product_specifications": {
      "type": "object",
      "required": [
        "category_profile",
        "normalized",
        "raw"
      ],
      "properties": {
        "category_profile": {
          "type": "string",
          "description": "Name of the profile that defines the keys of 'normalized'.",
          "examples": [
            "laptop",
            "refrigerator",
            "air_conditioner",
            "mobile"
          ]
        },
        "normalized": {
          "type": "object",
          "description": "Clean, comparable fields. Keys come from the category profile; unknown values are null.",
          "additionalProperties": {
            "type": [
              "string",
              "number",
              "boolean",
              "null"
            ]
          },
          "examples": [
            {
              "processor": "Intel Core i5-1235U",
              "ram": "16 GB DDR4",
              "storage": "512 GB SSD",
              "display": "15.6 inch",
              "operating_system": "Windows 11",
              "graphics": "Intel Iris Xe",
              "color": "Silver"
            },
            {
              "capacity_litres": 253,
              "door_type": "Double Door",
              "energy_rating": "3 Star",
              "compressor": "Inverter",
              "color": "Steel"
            },
            {
              "tonnage": 1.5,
              "ac_type": "Split",
              "energy_rating": "5 Star",
              "inverter": true,
              "cooling_capacity": "5000 W"
            },
            {
              "ram": "8 GB",
              "storage": "128 GB",
              "display_size": "6.7 inch",
              "battery": "5000 mAh",
              "rear_camera": "50 MP",
              "operating_system": "Android 14",
              "network": "5G"
            }
          ]
        },
        "raw": {
          "type": "object",
          "description": "Every key/value Amazon lists, unmodified. Nothing is lost if a field is not normalized yet.",
          "additionalProperties": {
            "type": "string"
          }
        }
      }
    },
    "platform_specific": {
      "type": "object",
      "properties": {
        "is_sponsored_listing": {
          "type": "boolean"
        }
      }
    },
    "scraping_metadata": {
      "type": "object",
      "required": [
        "scraped_at",
        "scraping_status"
      ],
      "properties": {
        "scraped_at": {
          "type": "string",
          "format": "date-time"
        },
        "last_updated": {
          "type": "string",
          "format": "date-time"
        },
        "scraping_status": {
          "type": "string",
          "enum": [
            "success",
            "partial",
            "failed"
          ]
        },
        "data_summary": {
          "type": "object",
          "properties": {
            "images_scraped": {
              "type": "integer"
            },
            "videos_scraped": {
              "type": "integer"
            },
            "reviews_scraped": {
              "type": "integer"
            },
            "variants_scraped": {
              "type": "integer"
            },
            "specifications_scraped": {
              "type": "integer"
            }
          }
        },
        "errors": {
          "type": "array",
          "items": {
            "type": "string"
          }
        }
      }
    }
  }
}
''')
VALIDATOR = Draft202012Validator(PRODUCT_SCHEMA, format_checker=FormatChecker())


def validate(record):
    problems = [f"schema: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
                for e in VALIDATOR.iter_errors(record)]
    if problems:
        record["scraping_metadata"]["errors"] += problems
        record["scraping_metadata"]["scraping_status"] = "partial"
    return problems


# ======================================================================================
# Output: one JSON array file per category, rewritten safely after every product
# ======================================================================================
class JsonStore:
    def __init__(self, path: Path):
        self.path, self.items = path, {}
        if path.exists():
            try:
                for rec in json.loads(path.read_text(encoding="utf-8")):
                    self.items[rec["asin"]] = rec
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                backup = path.with_suffix(".broken.json")
                path.replace(backup)
                log.warning("Could not read %s (%s); moved it to %s", path, e, backup)

    def __contains__(self, asin):
        return asin in self.items

    def put(self, rec):
        self.items[rec["asin"]] = rec
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.items.values()), indent=4, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


# ======================================================================================
# Crawl
# ======================================================================================
def scrape_one(browser, asin, domain, cat, sponsored, args):
    html = browser.get(f"https://www.{domain}/dp/{asin}", wait_for="#productTitle")
    review_info = {"reviews": [], "total_reviews": None, "login_required": False}
    if args.review_pages > 0 and re.search(r'data-hook="review"|acrCustomerReviewText', html):
        r, _pages, total, login = collect_review_pages(browser, asin, domain, args.review_pages)
        review_info.update(reviews=r, total_reviews=total, login_required=login)
    rec, crumbs = build_record(html, asin, domain, cat, sponsored, review_info)
    problems = validate(rec)
    if problems and list(VALIDATOR.iter_errors(rec)):  # still invalid -> don't save it
        raise ValueError("; ".join(problems))
    return rec, crumbs


def run_category(args, key, browser):
    cat, domain = CATEGORIES[key], args.domain
    out = Path(f"{key}_{domain.replace('.', '_')}.json")
    failed = out.with_suffix(".failed.jsonl")
    store = JsonStore(out)
    written = skipped = 0

    def handle(asin, sponsored):
        nonlocal written, skipped
        try:
            rec, crumbs = scrape_one(browser, asin, domain, cat, sponsored, args)
        except Exception as e:  # noqa: BLE001
            log.error("FAILED %s: %s", asin, e)
            with failed.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"asin": asin, "error": str(e), "at": now_iso()}) + "\n")
            return
        ok, why = product_matches(rec, crumbs, cat)
        if not ok:
            skipped += 1
            log.info("skip %s: %s", asin, why)
            return
        store.put(rec)
        written += 1
        p = rec["product_details"]["pricing"]
        s = rec["scraping_metadata"]["data_summary"]
        log.info("[%s %d] %s %s | %s %s | %d specs, %d images, %d reviews | %s", cat["label"], written, asin,
                 rec["scraping_metadata"]["scraping_status"], p["currency"], p["current_price"],
                 s["specifications_scraped"], s["images_scraped"], s["reviews_scraped"],
                 rec["product_details"]["title"][:60])

    if args.asin:
        for a in args.asin:
            handle(a.strip().upper(), False)
        return

    page_url, pages, seen = search_url(key, domain), 0, set()
    log.info("=== %s: %s", cat["label"], page_url)
    while page_url and pages < args.max_pages and written < args.max_products:
        items, page_url = parse_search_page(
            browser.get(page_url, wait_for="div[data-component-type='s-search-result']", full_scroll=False), domain)
        pages += 1
        log.info("search page %d: %d results", pages, len(items))
        if not items:
            break
        for it in items:
            if written >= args.max_products:
                break
            if it["asin"] in seen or (it["asin"] in store and not args.refresh):
                continue
            seen.add(it["asin"])
            if not card_matches(it, cat):
                skipped += 1
                continue
            handle(it["asin"], it["sponsored"])
    log.info("=== %s done: %d saved to %s, %d skipped (other brands/accessories)%s", cat["label"], written,
             out.resolve(), skipped, f", failures in {failed}" if failed.exists() else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", choices=list(CATEGORIES) + ["all"], default=CATEGORY_TO_RUN)
    ap.add_argument("--domain", default="amazon.in")
    ap.add_argument("--max-pages", type=int, default=SEARCH_PAGES, help="search result pages per category")
    ap.add_argument("--max-products", type=int, default=MAX_PRODUCTS_PER_CATEGORY, help="products per category")
    ap.add_argument("--review-pages", type=int, default=REVIEW_PAGES_PER_PRODUCT,
                    help="review pages per product (0 = product page only)")
    ap.add_argument("--asin", nargs="+", help="scrape these ASINs (use with one --category)")
    ap.add_argument("--refresh", action="store_true", default=REFRESH_SAVED_PRODUCTS,
                    help="re-scrape products already saved")
    ap.add_argument("--cdp", help="attach to your open Chrome, e.g. http://localhost:9222")
    ap.add_argument("--min-delay", type=float, default=MIN_DELAY_SECONDS)
    ap.add_argument("--max-delay", type=float, default=MAX_DELAY_SECONDS)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="fast run: search page 1 only, 1 review page per product, shorter delays")
    ap.add_argument("--show-urls", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.show_urls:
        for k in CATEGORIES:
            print(f"{k:17} {search_url(k, args.domain)}")
        return
    if args.quick:
        args.max_pages, args.review_pages = 1, 1
        args.min_delay, args.max_delay = 2.0, 4.0
    if args.asin and args.category == "all":
        ap.error("--asin needs one --category")

    browser = Browser(args.domain, delay=(args.min_delay, args.max_delay), headless=args.headless,
                      cdp_url=args.cdp)
    try:
        if browser.is_logged_in():
            log.info("Using your signed-in Amazon session.")
        else:
            log.warning("This browser is not signed in to Amazon.")
            browser.ensure_login()
        keys = list(CATEGORIES) if args.category == "all" else [args.category]
        for key in keys:
            run_category(args, key, browser)
    except KeyboardInterrupt:
        log.info("Stopped. Everything scraped so far is saved; re-run to continue.")
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())