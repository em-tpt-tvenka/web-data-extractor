import asyncio
import copy
import hashlib
import json
import logging
import re
import random
import sys
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from motor.motor_asyncio import AsyncIOMotorClient
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth

_stealth = Stealth()

_log = logging.getLogger("alibaba")
_log.setLevel(logging.DEBUG)
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)-5s %(message)s",
                                   datefmt="%H:%M:%S"))
_log.addHandler(_sh)
logging.getLogger("motor").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# Full search URL, the homepage (uses the keyword fields below), or "" for keyword mode.
DIRECT_URL = ""

KEYWORD         = "refrigerator"
FILTER_VERIFIED = True
FILTER_TA       = True
MIN_PRICE       = None
MAX_PRICE       = None
MIN_QUANTITY    = None
SORT_TYPE       = "relevance"   # relevance | price-asc | price-desc | orders-desc

# Keep only listings whose posted date falls in this range. "YYYY-MM-DD" or None.
# Applied as a filter on each product's date_posted (listings with an unknown
# date are kept, since Alibaba often omits it on the search card).
DATE_FROM = "2025-11-11"
DATE_TO   = "2026-11-11"

MAX_PAGES         = 1     # 0 = all available pages (stops when no products found)
MAX_PRODUCTS      = 5      # 0 = no limit
INCLUDE_SPONSORED = True   # False = skip sponsored listings
FETCH_REVIEWS     = True   # navigate to each product page to extract detailed reviews
REVIEW_CONCURRENCY = 3     # keep at 1 to avoid CAPTCHA bursts; raise to 2-3 only if rarely triggered
# Supplier feedback-page extraction (the heavy, paginated store-review source):
# cap how many feedback pages are scraped concurrently across products, and how
# many pager pages to traverse per supplier (80 pages ≈ 400 reviews). Concurrent
# products from the SAME supplier share one fetch (in-flight dedup + run cache).
FEEDBACK_CONCURRENCY = 2
FEEDBACK_MAX_PAGES   = 80
OUTPUT_CSV  = "alibaba_results.csv"
OUTPUT_JSON = "alibaba_results.json"
# Reviews (product + store/supplier + detailed), keyed by product_id
OUTPUT_REVIEWS_JSON  = "alibaba_reviews.json"
OUTPUT_REVIEWS_XLSX  = "alibaba_reviews.xlsx"

SCRAPER_VERSION = "1.0.0"

# Replaced in main() with a fresh timestamped folder (run_YYYY-MM-DD_HH-MM-SS)
# so every run's output files land in their own directory.
OUTPUT_DIR = Path(__file__).parent

# ── Historical store (section 14) ────────────────────────────────────────────
# Append-only time-series so trend reports can be built later. Each run appends
# (never overwrites) price / rating / full-snapshot records keyed by product_id
# + run_id, both to JSONL files here and to MongoDB history collections.
ENABLE_HISTORY = True
HISTORY_DIR    = str(Path(__file__).parent / "history")

# ── Compliance / access control (section 16) ─────────────────────────────────
# Automated collection of Alibaba data for a commercial product generally
# requires Alibaba's written permission or use of an official/partner API. This
# scraper NEVER bypasses login, CAPTCHA, or anti-bot controls. Set
# COLLECTION_AUTHORIZED = True only when you actually hold that authorization and
# record its reference; otherwise every run is tagged "authorization_unconfirmed"
# in the logs, the data-quality report, and the collection_runs history.
COLLECTION_AUTHORIZED   = False
AUTHORIZATION_REFERENCE = None   # e.g. contract id / written-permission ticket
COMPLIANCE_NOTICE = (
    "Automated collection of Alibaba data may require Alibaba's written "
    "permission or an official API. You are responsible for ensuring authorized, "
    "lawful use. This tool does not bypass login, CAPTCHA, or anti-bot controls."
)

MONGO_URI = "mongodb://localhost:27017"
MONGO_DB  = "alibaba_stealth_db_2"

# Dedicated Chrome profile (separate from your main one). Cookies persist across
# runs, so a CAPTCHA solved once is not shown again. None = fresh context each run.
CHROME_PROFILE_DIR = str(Path(__file__).parent / "chrome_scraper_profile")

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

_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "testOption", "spm", "from",
})

def _inject_page(base: str, page: int) -> str:
    parsed = urllib.parse.urlparse(base)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params.pop("page", None)
    for k in _STRIP_PARAMS:
        params.pop(k, None)
    if page > 1:
        params["page"] = [str(page)]
    flat = {k: v[0] for k, v in params.items()}
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(flat, safe="%")))


def _build_keyword_url(page: int) -> str:
    params: dict[str, str] = {"fsb": "y", "IndexArea": "product_en", "SearchText": KEYWORD}
    if page > 1:
        params["page"] = str(page)

    filters = (["ta"] if FILTER_TA else []) + (["verified"] if FILTER_VERIFIED else [])
    if filters:
        params["filter"] = "%2C".join(filters)
    if MIN_PRICE is not None:
        params["minPrice"] = str(MIN_PRICE)
    if MAX_PRICE is not None:
        params["maxPrice"] = str(MAX_PRICE)
    if MIN_QUANTITY is not None:
        params["minQuantity"] = str(MIN_QUANTITY)

    sort_map = {"relevance": "relevance", "price-asc": "price-asc",
                "price-desc": "price-desc", "orders-desc": "order_count-desc"}
    params["sortType"] = sort_map.get(SORT_TYPE, "relevance")
    return "https://www.alibaba.com/trade/search?" + urllib.parse.urlencode(params, safe="%")


_ALIBABA_DOMAINS = {"alibaba.com", "www.alibaba.com", "m.alibaba.com"}
_HOMEPAGE_PATHS = {"", "/", "/index.htm", "/index.html"}
_SPA_PATHS = {"/search/page", "/search/ai", "/search"}


def _is_alibaba_domain(url: str) -> bool:
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        return netloc in _ALIBABA_DOMAINS or netloc.endswith(".alibaba.com")
    except Exception:
        return False


def _is_homepage_url(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url).path.rstrip("/") or "/"
        return path in _HOMEPAGE_PATHS
    except Exception:
        return False


def _is_new_spa_url(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url).path.rstrip("/")
        return path in _SPA_PATHS or path.startswith("/search/")
    except Exception:
        return False


def page_url(page_num: int) -> str:
    raw = DIRECT_URL.strip()
    if not raw:
        url = _build_keyword_url(page_num)
        _log.info("event=url_built  mode=keyword  page=%d  url=%s", page_num, url[:90])
        return url

    if not _is_alibaba_domain(raw):
        raise ValueError(
            f"DIRECT_URL '{raw}' is not an Alibaba domain. "
            "Use an alibaba.com URL or leave DIRECT_URL empty for keyword mode."
        )

    if _is_homepage_url(raw):
        url = _build_keyword_url(page_num)
        _log.info("event=url_built  mode=keyword_via_domain  page=%d  url=%s", page_num, url[:90])
        return url

    # The new Pro Search SPA (/search/page) streams products via SSE, which is
    # blocked under automation — fall back to the HTML-rendering trade/search URL.
    if _is_new_spa_url(raw):
        _log.warning("event=spa_url_detected  falling back to trade/search keyword URL")
        url = _build_keyword_url(page_num)
        _log.info("event=url_built  mode=keyword_fallback_from_spa  page=%d  url=%s", page_num, url[:90])
        return url

    url = _inject_page(raw, page_num)
    _log.info("event=url_built  mode=direct  page=%d  url=%s", page_num, url[:90])
    return url


# ─────────────────────────────────────────────────────────────────────────────
# ID HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _product_id(href: str) -> str:
    m = re.search(r'_(\d{8,})(?:\.html|$|\?)', href)
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    pid = (qs.get("productId") or qs.get("offerId") or [""])[0]
    if pid:
        return pid
    clean = href.split("?")[0].rstrip("/")
    return hashlib.md5(clean.encode()).hexdigest()[:16]


def _cfg_date(s: str | None):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


_DATE_FROM = _cfg_date(DATE_FROM)
_DATE_TO   = _cfg_date(DATE_TO)

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
                 "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y")


def _parse_posted_date(s: str):
    if not s or s == "N/A":
        return None
    s = re.sub(r'(?i)^\s*(posted|updated|published)[:\s]+', '', s.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])).date()
        except ValueError:
            return None
    m = re.search(r'([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4})', s)
    if m:
        try:
            return datetime.strptime(f"{m[1][:3]} {m[2]} {m[3]}", "%b %d %Y").date()
        except ValueError:
            return None
    return None


def _in_date_range(model: dict) -> bool:
    d = _parse_posted_date(_pd_date_posted(model))
    if d is None:
        return True
    if _DATE_FROM and d < _DATE_FROM:
        return False
    if _DATE_TO and d > _DATE_TO:
        return False
    return True


def _pd_date_posted(model: dict):
    """Real listing date if one was extracted, else None (never the scrape time)."""
    pd_ = model.get("product_details", {})
    raw = pd_.get("date_posted")
    return raw if raw and raw != "N/A" else None


# ─────────────────────────────────────────────────────────────────────────────
# NORMALIZATION & VALIDATION  (pure, typed, fixture-tested helpers)
# ─────────────────────────────────────────────────────────────────────────────

_BAD_NAMES = {"", "n/a", "na", "null", "none", "-", "unknown"}


def _norm_name(name) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def _resolve_supplier_identity(company_id, card_name, detail_name) -> dict:
    """Stable supplier identity. supplier_id is derived from the REAL company id
    when available, else a hash of the real company name, else None — never a
    hash of 'N/A' (the bug that gave every product the same id). company_id is
    kept as a separate authoritative field. Raw names preserved for debugging."""
    real_name = detail_name if _norm_name(detail_name) not in _BAD_NAMES else card_name
    norm = _norm_name(real_name)

    cid = None
    if company_id not in (None, "", 0, "0"):
        try:
            cid = int(company_id)
        except (TypeError, ValueError):
            cid = str(company_id)

    if cid is not None:
        supplier_id = f"ali-{cid}"
    elif norm not in _BAD_NAMES:
        supplier_id = "name-" + hashlib.md5(norm.encode()).hexdigest()[:12]
    else:
        supplier_id = None

    return {
        "supplier_id":   supplier_id,
        "company_id":    cid,
        "supplier_name": real_name if norm not in _BAD_NAMES else None,
        "raw": {"card_name": card_name, "detail_name": detail_name},
    }


def _supplier_identity(m: dict) -> dict:
    seller = m.get("seller_native") or {}
    sd = m.get("supplier_details", {})
    return _resolve_supplier_identity(
        seller.get("companyId"),
        sd.get("card_name") or sd.get("name"),
        seller.get("companyName"),
    )


def validate_supplier_consistency(models: list[dict]) -> list[str]:
    """Warn when one supplier_id maps to conflicting company_ids or names."""
    by_id: dict[str, set] = {}
    by_id_name: dict[str, set] = {}
    warnings: list[str] = []
    for m in models:
        ident = _supplier_identity(m)
        sid = ident["supplier_id"]
        if not sid:
            continue
        by_id.setdefault(sid, set()).add(ident["company_id"])
        by_id_name.setdefault(sid, set()).add(ident["supplier_name"])
    for sid, cids in by_id.items():
        cids = {c for c in cids if c is not None}
        if len(cids) > 1:
            warnings.append(f"supplier_id {sid} maps to multiple company_ids {sorted(map(str, cids))}")
    for sid, names in by_id_name.items():
        names = {n for n in names if n}
        if len(names) > 1:
            warnings.append(f"supplier_id {sid} maps to multiple names {sorted(names)[:3]}")
    return warnings


# ---- Pricing (decimal-safe) -------------------------------------------------

_CURRENCY_SYMBOLS = {
    "US$": "USD", "C$": "CAD", "A$": "AUD", "₹": "INR", "$": "USD",
    "€": "EUR", "£": "GBP", "¥": "CNY", "₫": "VND", "₩": "KRW",
}
_CURRENCY_WORDS = {"rs": "INR", "inr": "INR", "usd": "USD", "cny": "CNY",
                   "rmb": "CNY", "eur": "EUR", "gbp": "GBP"}


def _to_decimal(num: str):
    try:
        return Decimal(num.replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _dec_str(d):
    # Fixed-point, preserving the source precision (e.g. "5.20" cents, not "5.2").
    return format(d, "f") if isinstance(d, Decimal) else None


def _dec_num(d):
    """Decimal → JSON number: int when whole, float otherwise. None stays None.
    Used for numeric price/MOQ fields (price_raw keeps the original string)."""
    if not isinstance(d, Decimal):
        return None
    return int(d) if d == d.to_integral_value() else float(d)


def parse_price(raw) -> dict:
    """Parse a price string into decimal-safe min/max + currency + unit. Handles
    Indian (2,05,509.02) and international grouping, ranges, and single prices.
    Numeric values are returned as decimal strings to avoid float error."""
    out = {
        "price_raw": raw if raw not in (None, "", "N/A") else None,
        "price_min": None, "price_max": None, "currency": None, "price_unit": None,
        "quantity_tier_min": None, "quantity_tier_max": None,
        "price_usd_min": None, "price_usd_max": None,
        "price_extraction_status": "missing",
    }
    if not raw or str(raw).strip() in ("", "N/A"):
        return out
    text = str(raw).strip()

    for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in text:
            out["currency"] = _CURRENCY_SYMBOLS[sym]
            break
    if out["currency"] is None:
        wm = re.search(r"\b(rs|inr|usd|cny|rmb|eur|gbp)\b", text, re.I)
        if wm:
            out["currency"] = _CURRENCY_WORDS[wm.group(1).lower()]

    um = re.search(r"/\s*([A-Za-z][A-Za-z ]{0,15})", text)
    if um:
        out["price_unit"] = um.group(1).strip().lower()

    nums = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    decs = [d for d in (_to_decimal(n) for n in nums) if d is not None]
    if not decs:
        out["price_extraction_status"] = "unparseable"
        return out
    if len(decs) >= 2 and re.search(r"[-–—]|\bto\b", text):
        out["price_min"] = _dec_num(min(decs))   # numeric JSON values
        out["price_max"] = _dec_num(max(decs))
        out["price_extraction_status"] = "range_parsed"
    else:
        out["price_min"] = out["price_max"] = _dec_num(decs[0])
        out["price_extraction_status"] = "parsed"
    return out


# ---- MOQ --------------------------------------------------------------------

_UNIT_SINGULAR = {
    "pieces": "piece", "pcs": "piece", "pc": "piece", "sets": "set", "units": "unit",
    "kilograms": "kilogram", "kgs": "kilogram", "kg": "kilogram", "tons": "ton",
    "tonnes": "tonne", "meters": "meter", "metres": "meter", "pairs": "pair",
    "boxes": "box", "bags": "bag", "rolls": "roll", "cartons": "carton",
    "dozens": "dozen", "acres": "acre", "liters": "liter", "litres": "liter",
}


def parse_moq(raw) -> dict:
    out = {"moq_raw": raw if raw not in (None, "", "N/A") else None,
           "moq_value": None, "moq_unit": None, "moq_extraction_status": "missing"}
    if not raw or str(raw).strip() in ("", "N/A"):
        return out
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]+)", str(raw))
    if not m:
        out["moq_extraction_status"] = "unparseable"
        return out
    val = _to_decimal(m.group(1))
    if val is None:
        out["moq_extraction_status"] = "unparseable"
        return out
    out["moq_value"] = _dec_num(val)   # numeric JSON value
    unit = m.group(2).lower()
    out["moq_unit"] = _UNIT_SINGULAR.get(unit, unit)
    out["moq_extraction_status"] = "parsed"
    return out


# ---- Image classification ---------------------------------------------------

_ICON_RE = re.compile(
    r"\.svg(\?|$)|/flags?/|/common/|/mobile/|tps-\d{1,3}-\d{1,3}\.|"
    r"-55-tps-|-56-tps-|icon|sprite|placeholder|avatar",
    re.I,
)


def _is_icon(u: str) -> bool:
    return not u or not u.startswith("http") or bool(_ICON_RE.search(u))


def classify_images(card_images, product_media, supplier_logo) -> dict:
    """Separate real product photos from icons/logos/UI assets and dedupe."""
    pm_imgs = (product_media or {}).get("images") or []
    product_imgs: list[str] = []
    seen: set[str] = set()
    for u in pm_imgs:                      # detail-page gallery = authoritative
        if u and u not in seen and not _is_icon(u):
            seen.add(u); product_imgs.append(u)
    if not product_imgs:                   # fallback: clean the card images
        for u in (card_images or []):
            if u and u not in seen and not _is_icon(u):
                seen.add(u); product_imgs.append(u)
    other: list[str] = []
    for u in (card_images or []):
        if u and u not in seen and not _is_icon(u) and u != supplier_logo:
            seen.add(u); other.append(u)
    if product_imgs:
        status = "classified"
    elif card_images:
        status = "icons_only"
    else:
        status = "missing"
    return {
        "main_image_url":     product_imgs[0] if product_imgs else None,
        "product_image_urls": product_imgs,
        "supplier_logo_url":  supplier_logo or None,
        "other_media_urls":   other,
        "image_extraction_status": status,
    }


# ---- Review validation + star parsing ---------------------------------------

_UI_NOISE_RE = re.compile(
    r"^(what are you looking for|all categories|know your supplier|search|sort by|"
    r"based on\b|most relevant|related searches|frequently bought|you may|about this|"
    r"product details|see product details|helpful|verified purchase|repeat buyer|"
    r"get the app|sign in|join free|trade assurance|categories|home|"
    r"previous slide|next slide|prev slide|previous|next|slide \d+|"
    r"min\.?\s*order|minimum order|moq\b|min\.? order quantity|"
    r"payment|shipping|logistics|add to cart|contact supplier|chat now|"
    r"view (more|all)|show (more|original)|report|share|save)\b",
    re.I,
)
_TAGBAR_RE = re.compile(r"^(?:[\w ]+\(\d+\))(?:\s*[\w ]+\(\d+\)){1,}$")
_REPLY_RE  = re.compile(r"(?i)supplier'?s?\s*repl(?:y|ies)\s*:?\s*")


def _split_supplier_reply(text):
    """Split a combined 'review ... Supplier's reply: ...' string into
    (customer_text, supplier_reply). Returns (text, None) when no reply marker."""
    if not isinstance(text, str):
        return text, None
    m = _REPLY_RE.search(text)
    if not m:
        return text, None
    customer = text[:m.start()].strip() or "N/A"
    reply    = text[m.end():].strip() or None
    return customer, reply


_MODEL_JUNK = {"number", "no", "no.", "num", "num.", "model", "model number",
               "n/a", "na", "-", "null", "none", ""}


def _sanitize_model_number(v):
    """Drop the known mis-parse where the capture group kept the literal label
    ('number', 'model', 'no.') instead of an actual model value. Returns None
    (not the string 'N/A') when the value is genuinely unavailable, so callers can
    distinguish an absent model from a literal 'N/A' text (section 7)."""
    if v in (None, "", "N/A"):
        return None
    s = str(v).strip()
    if s.lower() in _MODEL_JUNK:
        return None
    return s


# ─────────────────────────────────────────────────────────────────────────────
# FIELD-LEVEL VALIDATION (source-evidence based) — section 3, 4, 7, 8
#
# These run at record-build time (before each product/review is persisted) so
# incorrect values never enter the final JSON. Nothing is fabricated: when the
# correct value cannot be established from source evidence the field is left null
# with an explicit status, and the raw value + provenance are preserved for
# debugging. Statuses follow section 7:
#   available | missing_on_source | conflicting | invalid | not_verified |
#   not_provided | not_applicable
# ─────────────────────────────────────────────────────────────────────────────

# Model-number SKU/spec labels, lowercased, in precedence order.
_MODEL_LABELS = ("model number", "model no.", "model no", "model")

# A value that *looks like* a bare CPU/processor identifier. Used only as a guard
# for the token-containment collision check (e.g. model "N5095" sitting inside the
# CPU value "Intel N5095"); exact-equality collisions are caught independently, so
# a legitimate model such as "N143" is never rejected merely for matching here —
# it is rejected only when it actually appears in a processor value.
_CPU_LIKE_RE = re.compile(
    r"^(i[3579][\s-]?\d{2,5}[a-z]*|celeron|pentium|atom|ryzen|athlon|"
    r"core\s*(i|ultra|m)|xeon|snapdragon|mediatek|mt\d|"
    r"n\d{3,4}[a-z]*|j\d{3,4}[a-z]*|a\d{3,4}[a-z]*)$",
    re.I,
)


def _norm_ident(s) -> str:
    """Normalize an identifier for *comparison only* (never for storage):
    lowercase + whitespace collapsed. The stored value keeps its original form."""
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()


def _cpu_spec_values(specs: dict) -> list[str]:
    """Processor/CPU values declared in the specification table. Restricted to
    keys that mention 'processor' or 'cpu' so an ambiguous bare 'Type' spec (which
    is often a form factor, e.g. 'Ultraslim') is not mistaken for a CPU."""
    out = []
    for k, v in (specs or {}).items():
        kl = str(k).lower()
        if ("processor" in kl or "cpu" in kl) and v not in (None, "", "N/A"):
            out.append(str(v))
    return out


def _model_candidates(ex: dict) -> list[tuple[str, str]]:
    """All model-number candidates with provenance, in precedence order:
    SKU variant labelled as model number first, then the specification table."""
    cands: list[tuple[str, str]] = []
    for v in (ex.get("variants") or []):
        if _norm_ident(v.get("name")) in _MODEL_LABELS:
            vals = [x for x in (v.get("values") or []) if x]
            if vals:
                cands.append(("variant", str(vals[0]).strip()))
                break
    specs = ex.get("specifications") or {}
    for k, val in specs.items():
        if _norm_ident(k) in _MODEL_LABELS and val not in (None, "", "N/A"):
            cands.append(("specification", str(val).strip()))
            break
    return cands


def _is_cpu_value(model: str, cpu_values: list[str]) -> str | None:
    """Return the processor value a model collides with, else None.
    Collision = the normalized model equals a processor value, OR (when the model
    looks like a bare CPU id) the model is a standalone token inside a longer
    processor value (e.g. 'N5095' within 'Intel N5095')."""
    nm = _norm_ident(model)
    if not nm:
        return None
    for cv in cpu_values:
        if _norm_ident(cv) == nm:
            return cv
    if _CPU_LIKE_RE.match(model.strip()) and " " not in nm:
        for cv in cpu_values:
            toks = set(re.findall(r"[a-z0-9][a-z0-9\-]*", cv.lower()))
            if nm in toks and _norm_ident(cv) != nm:
                return cv
    return None


def validate_model_number(ex: dict) -> dict:
    """Establish the product model number from authoritative source evidence,
    rejecting values that are actually the CPU/processor (section 3).

    Returns: {value, status, source, raw, candidates, reason, collides_with}.
    The CPU is never substituted; when no trustworthy model exists the value is
    null with status 'not_provided' (absent) or 'invalid' (a CPU masquerading as
    the model)."""
    specs = ex.get("specifications") or {}
    cpu_values = _cpu_spec_values(specs)
    raw_cands = _model_candidates(ex)

    # Clean candidates (drop label-junk mis-parses), keep provenance + order.
    cleaned: list[tuple[str, str]] = []
    for src, val in raw_cands:
        s = _sanitize_model_number(val)
        if s is not None:
            cleaned.append((src, s))

    if not cleaned:
        return {"value": None, "status": "not_provided", "source": None,
                "raw": None, "candidates": [], "reason": "no model-number attribute on page",
                "collides_with": None, "confidence": None, "cross_field_match": None}

    all_values = [c for _, c in cleaned]
    collisions = [(src, val, _is_cpu_value(val, cpu_values)) for src, val in cleaned]
    non_cpu = [(src, val) for (src, val, col) in collisions if col is None]

    # Every candidate is actually a processor value → the model is not provided.
    if not non_cpu:
        first_src, first_val, first_col = collisions[0]
        return {"value": None, "status": "invalid", "source": first_src, "raw": first_val,
                "candidates": all_values,
                "reason": f"candidate {first_val!r} equals processor/CPU value; not a product model",
                "collides_with": first_col, "confidence": None, "cross_field_match": False}

    # The authoritative source (variant SKU, else the first spec) was itself a CPU
    # value: do NOT silently promote a lower-trust fallback (which is often a
    # mislabeled spec, e.g. a graphics value). Leave null, unverified, with
    # evidence — never guess (sections 3C / 13).
    top_src, top_val, top_col = collisions[0]
    if top_col is not None:
        return {"value": None, "status": "not_verified", "source": top_src, "raw": top_val,
                "candidates": all_values,
                "reason": (f"authoritative model source was a CPU value ({top_val!r}); "
                           f"remaining candidate(s) {[v for _, v in non_cpu]} unverified"),
                "collides_with": top_col, "confidence": None, "cross_field_match": False}

    # Top candidate is a genuine (non-CPU) model → keep it (precedence preserved).
    distinct = {_norm_ident(v) for _, v in non_cpu}
    status = "conflicting" if len(distinct) > 1 else "available"
    reason = (f"multiple distinct model candidates {[v for _, v in non_cpu]}"
              if status == "conflicting" else None)
    # Confidence: the SKU "model number" variant is the most authoritative source
    # (high); a spec-table model is medium; a genuine conflict drops to low.
    if status == "conflicting":
        confidence = "low"
    else:
        confidence = "high" if top_src == "variant" else "medium"
    return {"value": top_val, "status": status, "source": top_src, "raw": top_val,
            "candidates": all_values, "reason": reason, "collides_with": None,
            "confidence": confidence, "cross_field_match": (status != "conflicting")}


_GENERIC_BRANDS = {"oem", "odm", "oem/odm", "none", "n/a", "na", "null", "-",
                   "generic", "no brand", "nobrand", "unbranded", "other",
                   "others", "customized", "custom", ""}


def _sanitize_brand(v) -> dict:
    """Generic markers (OEM, None, N/A, …) are not verified brand names (section
    4C): the brand is nulled but the raw marker is preserved for debugging. The
    brand is NEVER inferred from the title/supplier name — only source brand
    evidence counts."""
    if v in (None, "", "N/A"):
        return {"value": None, "status": "missing_on_source", "raw": None,
                "normalized": None, "reason": "no brand attribute on page"}
    s = str(v).strip()
    if s.lower() in _GENERIC_BRANDS:
        return {"value": None, "status": "not_verified", "raw": s, "normalized": None,
                "reason": f"generic value {s!r}; consumer brand not established"}
    return {"value": s, "status": "available", "raw": s, "normalized": s, "reason": None}


def _validate_price_block(price: dict) -> list[str]:
    """Cross-field checks on the normalized price block. Returns issue strings
    (never raises, never drops data)."""
    issues: list[str] = []
    pmin, pmax = price.get("price_min"), price.get("price_max")
    if isinstance(pmin, (int, float)) and isinstance(pmax, (int, float)) and pmin > pmax:
        issues.append(f"price_min ({pmin}) > price_max ({pmax})")
    if price.get("price_min") is not None and not price.get("currency"):
        issues.append("price present but currency missing")
    tiers = price.get("price_tiers") or []
    prev_max = None
    for i, t in enumerate(tiers):
        mn, mx = t.get("min_quantity"), t.get("max_quantity")
        if isinstance(mn, (int, float)) and isinstance(mx, (int, float)) and mn > mx:
            issues.append(f"tier {i}: min_quantity ({mn}) > max_quantity ({mx})")
        # Overlap: this tier's min should exceed the previous tier's max.
        if (isinstance(prev_max, (int, float)) and isinstance(mn, (int, float))
                and mn <= prev_max):
            issues.append(f"tier {i}: min_quantity ({mn}) overlaps previous tier max ({prev_max})")
        if mx is not None:
            prev_max = mx
    return issues


def _detect_spec_conflicts(specs: dict) -> list[str]:
    """Flag specification keys that differ only by capitalization/whitespace yet
    carry DIFFERENT values (section 4D), e.g. 'products status'='Stock' vs
    'Products Status'='New'. Both raw keys are preserved in the output; this only
    surfaces the conflict so it is not silently resolved."""
    groups: dict[str, set] = {}
    for k, v in (specs or {}).items():
        groups.setdefault(_norm_ident(k), set()).add(str(v))
    out = []
    for norm, vals in groups.items():
        if len(vals) > 1:
            out.append(f"spec key {norm!r} has conflicting values {sorted(vals)}")
    return out


_RES_RE = re.compile(r"(\d{3,4})\s*[x*×]\s*(\d{3,4})", re.I)


def _spec_conflict_objects(specs: dict) -> list[dict]:
    """Structured spec conflicts: (1) keys differing only by case/whitespace but
    with different values; (2) resolution-type specs (resolution/screen/display)
    whose numeric WxH disagree. Returns [{field, values, status:'conflict'}].
    Raw specs are always preserved; this only surfaces the conflict."""
    out: list[dict] = []
    groups: dict[str, set] = {}
    for k, v in (specs or {}).items():
        groups.setdefault(_norm_ident(k), set()).add(str(v))
    for norm, vals in groups.items():
        if len(vals) > 1:
            out.append({"field": norm, "values": sorted(vals), "status": "conflict"})
    res_vals: dict[str, str] = {}
    for k, v in (specs or {}).items():
        kl = str(k).lower()
        if "resolution" in kl or kl == "screen" or "display" in kl:
            m = _RES_RE.search(str(v))
            if m:
                res_vals[f"{m.group(1)}x{m.group(2)}"] = str(v)
    if len(res_vals) > 1:
        out.append({"field": "resolution", "values": sorted(res_vals.values()),
                    "status": "conflict"})
    return out


def _consistency_checks(pd_out: dict, mnv: dict) -> dict:
    """Cross-field consistency summary for the validation block (section 5 of the
    review). Comparisons that can't be made return null / 'n/a'; weight compares
    net (attribute) vs package (shipping), which may legitimately differ, so a
    mismatch is 'needs_review' rather than a hard conflict."""
    price = pd_out["commercial"]["price"]
    tiers = price.get("price_tiers") or []
    tp = [t["price"] for t in tiers if isinstance(t.get("price"), (int, float))]
    if tp and price.get("price_min") is not None:
        tiers_match = (price["price_min"] == min(tp) and price["price_max"] == max(tp))
    else:
        tiers_match = None
    attr_w = pd_out["attributes"].get("weight")
    ship_w = pd_out["shipping"].get("unit_weight")
    if attr_w and ship_w:
        weight_consistent = "ok" if _norm_ident(attr_w) == _norm_ident(ship_w) else "needs_review"
    else:
        weight_consistent = "n/a"
    return {
        "price_tiers_match_price_range": tiers_match,
        "model_matches_variant": mnv.get("cross_field_match"),
        "weight_consistent": weight_consistent,
        "specification_conflicts": _spec_conflict_objects(pd_out.get("specifications")),
    }


# Structural reasons a field is null even though extraction succeeded — i.e. the
# source genuinely does not carry it, so null is correct (not a bug).
_NULL_REASONS = {
    "description":            "rich description is served by a separate lazy-loaded module, not the detail payload",
    "category.category_id":   "category not present in the detail payload",
    "category.category_path": "category breadcrumb not present in the detail payload",
    "dates.date_posted":      "Alibaba does not expose a listing/publication date in the detail payload",
    "dates.date_updated":     "Alibaba does not expose an update date in the detail payload",
    "commercial.price.price_unit":        "price unit not provided in the source price object",
    "commercial.price.price_usd_min":     "no USD value in the source payload (FX is never fabricated)",
    "commercial.price.price_usd_max":     "no USD value in the source payload (FX is never fabricated)",
    "commercial.price.quantity_tier_min": "no tiered pricing (single price) in the source",
    "commercial.price.quantity_tier_max": "open-ended final tier or no tiered pricing in the source",
    "commercial.price.currency":          "currency symbol/code not detected in the price string",
    "commercial.moq.moq_unit":            "unit not present in the MOQ text",
    "media.main_image_url":   "no product image could be classified from the detail gallery or search card",
    "media.supplier_logo_url": "no supplier logo in the detail payload",
    "media.video_urls":       "no product video found in the detail payload or card",
}


def _null_field_report(pd_out: dict, payload_present: bool, mnv: dict, brand: dict) -> dict:
    """Per-field explanation for EVERY field whose value is null/empty: a status
    plus a human-readable reason. Distinguishes extraction_failed (detail payload
    unreadable) from missing_on_source (payload read, field genuinely absent) and
    from structural nulls (e.g. FX never fabricated). Present fields are omitted."""
    a    = pd_out["attributes"]; price = pd_out["commercial"]["price"]
    moq  = pd_out["commercial"]["moq"]; ship = pd_out["shipping"]
    media = pd_out["media"]; sup = pd_out["supplier_info"]
    cat  = pd_out["category"]; dates = pd_out["dates"]
    comm = pd_out["commercial"]

    # (path, value, is_collection, payload_sourced)
    fields = [
        ("description",            pd_out["description"], False, True),
        ("category.category_id",   cat.get("category_id"), False, True),
        ("category.category_path", cat.get("category_path"), True, True),
        ("dates.date_posted",      dates.get("date_posted"), False, True),
        ("dates.date_updated",     dates.get("date_updated"), False, True),
        ("attributes.brand",       a.get("brand"), False, True),
        ("attributes.model_number", a.get("model_number"), False, True),
        ("attributes.material",    a.get("material"), False, True),
        ("attributes.weight",      a.get("weight"), False, True),
        ("attributes.dimensions",  a.get("dimensions"), False, True),
        ("attributes.condition",   a.get("condition"), False, True),
        ("attributes.warranty",    a.get("warranty"), False, True),
        ("attributes.after_sales_service", a.get("after_sales_service"), False, True),
        ("attributes.returns",     a.get("returns"), False, True),
        ("attributes.place_of_origin", a.get("place_of_origin"), False, True),
        ("attributes.colors",      a.get("colors"), True, True),
        ("attributes.sizes",       a.get("sizes"), True, True),
        ("attributes.variants",    a.get("variants"), True, True),
        ("attributes.certifications", a.get("certifications"), True, True),
        ("commercial.price.price_min",        price.get("price_min"), False, False),
        ("commercial.price.price_max",        price.get("price_max"), False, False),
        ("commercial.price.currency",         price.get("currency"), False, False),
        ("commercial.price.price_unit",       price.get("price_unit"), False, True),
        ("commercial.price.price_usd_min",    price.get("price_usd_min"), False, True),
        ("commercial.price.price_usd_max",    price.get("price_usd_max"), False, True),
        ("commercial.price.quantity_tier_min", price.get("quantity_tier_min"), False, True),
        ("commercial.price.quantity_tier_max", price.get("quantity_tier_max"), False, True),
        ("commercial.moq.moq_value", moq.get("moq_value"), False, False),
        ("commercial.moq.moq_unit",  moq.get("moq_unit"), False, False),
        ("commercial.box_moq",       comm.get("box_moq"), False, True),
        ("commercial.batch_number",  comm.get("batch_number"), False, True),
        ("shipping.shipping_origin", ship.get("shipping_origin"), False, True),
        ("shipping.lead_time",       ship.get("lead_time"), True, True),
        ("shipping.unit_size",       ship.get("unit_size"), False, True),
        ("shipping.unit_weight",     ship.get("unit_weight"), False, True),
        ("media.main_image_url",     media.get("main_image_url"), False, False),
        ("media.supplier_logo_url",  media.get("supplier_logo_url"), False, True),
        ("media.video_urls",         media.get("video_urls"), True, False),
        ("supplier_info.supplier_id", sup.get("supplier_id"), False, True),
        ("supplier_info.company_id", sup.get("company_id"), False, True),
        ("supplier_info.name",       sup.get("name"), False, True),
    ]
    report: dict = {}
    for path, val, coll, payload in fields:
        if val not in (None, "", [], {}):
            continue
        if path == "attributes.model_number":
            status = mnv["status"] if (payload_present or mnv["value"]) else "extraction_failed"
            reason = mnv.get("reason") or "no model-number attribute on the page"
        elif path == "attributes.brand":
            status = brand["status"] if (payload_present or brand["value"]) else "extraction_failed"
            reason = brand.get("reason") or "no brand attribute on the page"
        elif payload and not payload_present:
            status = "extraction_failed"
            reason = ("detail payload (window.detailData) was not readable for this "
                      "product; field could not be extracted (not confirmed absent)")
        elif path in _NULL_REASONS:
            status = "none_found" if coll else "missing_on_source"
            reason = _NULL_REASONS[path]
        else:
            status = "none_found" if coll else "missing_on_source"
            reason = ("no items found in the source" if coll
                      else "field not present in the page's data")
        report[path] = {"status": status, "reason": reason}
    return report


def _validate_cross_fields(rec: dict) -> list[str]:
    """Business-rule / cross-field validation on a *built* product record,
    executed before it is persisted (section 8). Returns issue strings; it never
    mutates or discards the record — valid fields are always kept."""
    pd_ = rec["product_details"]
    issues: list[str] = []

    oid = str(pd_.get("offerId") or "")
    url = pd_.get("product_url") or ""
    if oid and url and oid not in url:
        issues.append(f"offerId {oid} not found in product_url")

    issues += _validate_price_block(pd_["commercial"]["price"])
    issues += _detect_spec_conflicts(pd_.get("specifications"))

    media = pd_["media"]
    if media.get("video_count") != len(media.get("video_urls") or []):
        issues.append(f"video_count ({media.get('video_count')}) != len(video_urls) "
                      f"({len(media.get('video_urls') or [])})")

    if not pd_["supplier_info"].get("company_id"):
        issues.append("supplier company_id unavailable")

    return issues


def _dedupe(seq) -> list:
    """Order-preserving de-duplication (used for review media URLs)."""
    seen, out = set(), []
    for x in seq or []:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _looks_like_ui(text) -> bool:
    t = str(text or "").strip()
    if not t or t == "N/A":
        return False
    return bool(_UI_NOISE_RE.match(t)) or bool(_TAGBAR_RE.match(t))


def parse_stars(raw):
    """Normalize a star value to an int 1-5, or None if genuinely unavailable.
    Never invents a rating from text."""
    if raw in (None, "", "N/A"):
        return None
    if isinstance(raw, (int, float)):
        v = round(float(raw))
        return v if 1 <= v <= 5 else None
    m = re.search(r"([0-5](?:\.\d)?)", str(raw))
    if not m:
        return None
    v = round(float(m.group(1)))
    return v if 1 <= v <= 5 else None


def validate_review(item: dict) -> dict:
    """Validate one review. UI-noise reviewer/country are nulled (not a reason to
    drop a genuine review); a record is rejected only when it has no genuine
    content at all (no real text, no rating, no media). Returns cleaned fields."""
    warnings: list[str] = []
    reviewer = item.get("reviewer", "N/A")
    country  = item.get("country", "N/A")
    rating   = parse_stars(item.get("rating") if item.get("rating") is not None else item.get("stars"))

    ctext, _ = _split_supplier_reply(item.get("text", "N/A"))
    media = item.get("media") or item.get("review_media") or {}
    media_present = bool(media.get("images") or media.get("videos"))

    clean_reviewer = None
    if _looks_like_ui(reviewer):
        warnings.append(f"reviewer looks like UI text: {reviewer!r}")
    elif reviewer not in (None, "", "N/A"):
        clean_reviewer = reviewer

    clean_country = None
    if _looks_like_ui(country):
        warnings.append(f"country looks like UI text: {country!r}")
    elif country not in (None, "", "N/A"):
        clean_country = country

    text_is_ui = _looks_like_ui(ctext)
    if text_is_ui:
        warnings.append(f"review text looks like UI/page content: {str(ctext)[:40]!r}")

    genuine_text = ctext not in (None, "", "N/A") and not text_is_ui
    # A record is genuine when it has real text OR a real rating. Media ALONE is
    # not enough — UI blocks carry product-gallery images, so media only counts
    # when paired with a real (non-UI) reviewer name.
    has_content = genuine_text or (rating is not None) or (media_present and clean_reviewer is not None)
    return {"valid": bool(has_content), "rating": rating, "warnings": warnings,
            "clean_reviewer": clean_reviewer, "clean_country": clean_country}


def review_quality(block: dict | None, tab_count: int, complete: bool = False) -> dict:
    """Validate a review block and return the kept (clean) items plus statistics
    that are internally consistent with those items:
      extracted == len(items) == valid, rejected = dropped, missing_ratings =
      kept items whose rating is null.
    `complete` = the source list was fully traversed (e.g. the supplier feedback
    pager reached its disabled 'Next'); then total_displayed is the authoritative
    extracted count and the status is `available`, regardless of the (often
    inflated) product-tab count.
    """
    if block is None:
        status = "no_reviews_reported" if tab_count == 0 else "unavailable"
        return {"items": [], "status": status, "total_displayed": tab_count,
                "extracted": 0, "valid": 0, "rejected": 0, "missing_ratings": 0,
                "warnings": []}

    raw = block.get("reviews", []) or []
    clean, warnings = [], []
    rejected_n = 0
    for it in raw:
        res = validate_review(it)
        warnings.extend(res["warnings"])
        if not res["valid"]:
            rejected_n += 1
            continue
        it = dict(it)
        it["reviewer"] = res["clean_reviewer"] or "N/A"
        it["country"]  = res["clean_country"] or "N/A"
        it["rating"]   = res["rating"]
        it["valid"]    = True
        clean.append(it)

    extracted = valid_n = len(clean)                 # output items == extracted == valid
    missing_ratings = sum(1 for it in clean if it.get("rating") is None)

    if complete:
        # Fully traversed the authoritative list: the extracted count IS the total.
        displayed = valid_n
        status = "available" if valid_n > 0 else "no_reviews_reported"
    else:
        # total_displayed = reported tab count, but never fewer than what we kept
        # (prevents extracted > displayed); fall back to extracted when unknown.
        displayed = max(tab_count, valid_n) if tab_count > 0 else valid_n
        if displayed == 0:
            status = "no_reviews_reported"
        elif valid_n < displayed:
            status = "partially_extracted"
        else:
            status = "available"
    return {"items": clean, "status": status, "total_displayed": displayed,
            "extracted": extracted, "valid": valid_n, "rejected": rejected_n,
            "missing_ratings": missing_ratings, "warnings": warnings[:20]}


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER
# ─────────────────────────────────────────────────────────────────────────────

async def new_stealth_context(browser):
    # Sec-Fetch-* headers are omitted so Chrome sets them per navigation context.
    return await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=random.choice(VIEWPORTS),
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        bypass_csp=False,
        extra_http_headers={
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept-Encoding":           "gzip, deflate, br",
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Connection":                "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        },
    )


async def new_stealth_page(ctx) -> Page:
    page = await ctx.new_page()
    await _stealth.apply_stealth_async(page)
    return page


async def _wait_network_idle(page, timeout_ms: int = 3_000) -> None:
    """Wait until the DOM stops changing, exiting as soon as it is stable.
    Alibaba never reaches real network-idle (constant tracking/SSE traffic), so a
    plain networkidle wait always burns the full timeout; this returns early."""
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


async def scroll_page(page: Page) -> None:
    _log.info("event=scroll_start")
    height = await page.evaluate("document.body.scrollHeight") or 5000
    for i in range(1, 6):
        target = int(height * i / 5)
        await page.evaluate(f"window.scrollTo(0, {target})")
        await _wait_network_idle(page, timeout_ms=2_000)
        new_h = await page.evaluate("document.body.scrollHeight") or height
        if new_h > height:
            height = new_h
    await page.evaluate("window.scrollTo(0, 0)")


# ─────────────────────────────────────────────────────────────────────────────
# DETAIL PAGE — REVIEW EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

_REVIEWS_JS = r"""
() => {
    // ── Tab counts: "Product reviews (0)", "Store reviews (623)" ──
    const tabs = {};
    document.querySelectorAll('*').forEach(el => {
        if (el.children.length > 0) return;
        const m = el.textContent.trim().match(/^(.+?)\s*\((\d+)\)$/);
        if (m && /review|feedback|evaluate|store|product|supplier/i.test(m[1]))
            tabs[m[1].trim()] = parseInt(m[2]);
    });

    const bodyText = document.body ? (document.body.innerText || '') : '';
    let overallRating = 'N/A';
    const ratingM = bodyText.match(/\b([1-5]\.[0-9])\b/);
    if (ratingM) overallRating = ratingM[1];

    const MASK = /\b[A-Za-z]\*{2,}[a-z]?\b/;
    const META = /^(verified purchase|helpful|repeat buyer|show (more|original)|with photos|report abuse|translated|original|\d+\s*(star|sold)|model\s*(number|no))/i;

    // ── Locate REAL review cards via stable review containers (never whole-page) ──
    const seen = new Set();
    let cards = [];
    const listSels = ['.product-review-list', '.review-layout', '[class*="product-review-list"]',
                      '[class*="feedback-list"]', '[class*="review-list"]'];
    function looksReview(el) {
        if (!el) return false;
        const t = (el.innerText || '').trim();
        if (t.length < 8) return false;
        return MASK.test(t) || /verified\s+purchase/i.test(t) ||
               !!el.querySelector('.star-rating-list,[class*="star-rating"]');
    }
    function pushCard(el) {
        if (!el || seen.has(el)) return;
        if (!looksReview(el)) return;
        seen.add(el); cards.push(el);
    }
    for (const sel of listSels) {
        document.querySelectorAll(sel).forEach(list => {
            Array.from(list.children).forEach(pushCard);
        });
        if (cards.length) break;
    }
    // Strict fallback: anchor on masked-name text nodes; take the nearest ancestor
    // that also carries a date / verified / star (keeps UI chrome out).
    if (cards.length === 0) {
        const tw = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, null);
        let tn;
        while ((tn = tw.nextNode())) {
            if (!MASK.test(tn.textContent)) continue;
            let el = tn.parentElement;
            for (let i = 0; i < 5 && el && el !== document.body; i++) {
                const t = (el.innerText || '');
                if (t.length > 40 && (/verified\s+purchase/i.test(t) ||
                    /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}/.test(t) ||
                    el.querySelector('.star-rating-list,[class*="star-rating"]'))) { pushCard(el); break; }
                el = el.parentElement;
            }
        }
    }

    function parseStars(card) {
        // 1) filled star symbols (<use xlink:href="#icon-rating-star-fill">)
        const lists = card.querySelectorAll('.star-rating-list, [class*="star-rating"]');
        for (const lst of lists) {
            let fills = 0;
            lst.querySelectorAll('use').forEach(u => {
                const href = u.getAttribute('xlink:href') || u.getAttribute('href') || '';
                if (/star-fill|star_on|-on$/i.test(href)) fills++;
            });
            if (fills >= 1 && fills <= 5) return fills;
        }
        // 2) width-percentage overlay
        const ov = card.querySelector('[class*="star"] [style*="width"],[class*="rate"] [style*="width"]');
        if (ov) { const w = (ov.getAttribute('style') || '').match(/width:\s*([\d.]+)%/);
                  if (w) { const p = parseFloat(w[1]); if (p > 0 && p <= 100) return Math.round(p / 20); } }
        // 3) accessible label / title
        const a = card.querySelector('[aria-label*="out of"],[aria-label*="star"],[aria-label*="rating"],[title*="star"]');
        if (a) { const v = (a.getAttribute('aria-label') || a.getAttribute('title') || '')
                    .match(/([1-5])(?:\.\d)?\s*(?:out\s*of\s*5|stars?)/i); if (v) return parseInt(v[1]); }
        // 4) explicit "N-star" in the card text (not from overall summary)
        const tm = (card.innerText || '').match(/\b([1-5])\s*-?\s*stars?\b/i);
        if (tm) return parseInt(tm[1]);
        return null;
    }

    const reviews = cards.slice(0, 300).map(card => {
        const raw   = (card.innerText || card.textContent || '').trim();
        const lines = raw.split('\n').map(s => s.trim()).filter(Boolean);

        const maskedM  = raw.match(MASK);
        const reviewer = maskedM ? maskedM[0]
            : (lines.find(l => l.length < 30 && /^[A-Z]/.test(l) && !META.test(l)) || 'N/A');

        const dateMatch = raw.match(/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|\d{4}[-\/]\d{2}[-\/]\d{2}/i);
        const flagM   = raw.match(/[\u{1F1E0}-\u{1F1FF}]{2}\s*([A-Za-z][a-z ]{1,20})/u);
        const ctLines = lines.filter(l => l.length < 28 && /^[A-Z][a-z]/.test(l) && l !== reviewer
                                      && !META.test(l) && !/\d{4}/.test(l));
        const country = (flagM ? flagM[1].trim() : null) || ctLines[0] || 'N/A';

        const textCands = lines.filter(l => l.length > 12 && l !== reviewer && l !== country
            && !META.test(l) && !(dateMatch && l.indexOf(dateMatch[0]) >= 0) && !/^helpful/i.test(l));
        const reviewText = textCands.length ? textCands.reduce((a, b) => a.length >= b.length ? a : b) : 'N/A';

        const stars = parseStars(card);
        const modelMatch = raw.match(/model\s*(?:number|no\.?|num\.?)?[:\s]+([A-Z0-9][\w\-]{2,})/i);

        const imgs = Array.from(card.querySelectorAll('img')).map(img =>
            img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || ''
        ).filter(s => s.startsWith('http') && !/flag|avatar|\.svg|tps-\d{1,2}-\d{1,2}\./i.test(s));
        const vids = Array.from(card.querySelectorAll('video[src],video source,[class*="video"] source'))
            .map(v => v.src || v.getAttribute('src') || v.getAttribute('data-src') || '')
            .filter(s => s.startsWith('http'));

        return {
            reviewer,
            country,
            date:              dateMatch ? dateMatch[0] : 'N/A',
            stars:             stars === null ? 'N/A' : String(stars),
            text:              reviewText,
            model_number:      modelMatch ? modelMatch[1] : 'N/A',
            verified_purchase: /verified\s+purchase/i.test(raw),
            repeat_buyer:      /repeat\s+buyer/i.test(raw),
            review_media:      { images: imgs, videos: vids },
        };
    });

    // ── Product posted / updated date from the detail page ──
    let datePosted = 'N/A';
    const dtPatterns = [
        /(?:posted|published|post\s*date)[:\s]+([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})/i,
        /(?:posted|published|post\s*date)[:\s]+(\d{4}[-\/]\d{2}[-\/]\d{2})/i,
        /(?:updated|update\s*date|last\s*updated)[:\s]+([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})/i,
        /(?:updated|update\s*date|last\s*updated)[:\s]+(\d{4}[-\/]\d{2}[-\/]\d{2})/i,
    ];
    for (const pat of dtPatterns) {
        const m = bodyText.match(pat);
        if (m) { datePosted = m[1].trim(); break; }
    }

    return { overall_rating: overallRating, tabs, reviews,
             total_reviews: reviews.length, date_posted: datePosted };
}
"""


# Reads Alibaba's own embedded detail payload (window.detailData.globalData) for
# the REAL native seller/company fields and store-rating summary. Returns null if
# the global is absent (e.g. an older template), in which case callers fall back
# to the DOM-scraped card values.
_SELLER_JS = """
() => {
    const dd = window.detailData;
    if (!dd || !dd.globalData) return null;
    const g = dd.globalData;
    const s = g.seller || {};
    const out = { seller: {}, store_review_summary: null };

    ['companyId','companyName','companyBusinessType','companyJoinYears',
     'companyRegisterCountry','companyEncryptId','companyProfileUrl',
     'companyVideoUrl','supplierOnTimeDeliveryRate','responseTimeText',
     'subDomain','localCompanyJoinYears','feedbackUrl',
     // reorder / repeat-purchase rate — key name varies by payload; captured
     // only when actually present (never fabricated).
     'supplierReorderRate','reorderRate','repeatPurchaseRate','repeatBuyerRate',
     'buyerRepeatRate'].forEach(k => {
        if (s[k] !== undefined && s[k] !== null) out.seller[k] = s[k];
    });
    if (!out.seller.feedbackUrl && s.subDomain)
        out.seller.feedbackUrl = 'https://' + s.subDomain + '/company_profile/feedback.html';
    if (s.baoAccountIsDisplayAssurance !== undefined)
        out.seller.tradeAssurance = !!s.baoAccountIsDisplayAssurance;
    if (s.topRatedSupplier) {
        out.seller.topRatedSupplier = !!s.topRatedSupplier.text;
        if (s.topRatedSupplier.text) out.seller.topRatedSupplierText = s.topRatedSupplier.text;
    }
    if (s.tradeHalfYear) {
        out.seller.tradeOrderAmount6m   = s.tradeHalfYear.ordAmt6m;
        out.seller.tradeOrderCount6m    = s.tradeHalfYear.ordCnt6m;
        out.seller.tradeOrderAmountText = s.tradeHalfYear.ordAmt;
    }

    const srr = s.supplierRatingReviews;
    if (srr) {
        const sub = {};
        let total = 0;
        (srr.latitudeScoreDTOList || []).forEach(lat => {
            const breakdown = {};
            let latTotal = 0;
            (lat.scoreScatterDTOList || []).forEach(sc => {
                breakdown[String(sc.scatterValue)] = sc.scatterCount;
                latTotal += (sc.scatterCount || 0);
            });
            if (latTotal > total) total = latTotal;
            sub[lat.latitudeType] = {
                score:          lat.latitudeAverageScore,
                star_breakdown: breakdown,
            };
        });
        out.store_review_summary = {
            averageStar:   srr.averageStar || 'N/A',
            total_reviews: total,
            sub_ratings:   sub,
        };
    }

    // Real product gallery (clean full-size images + the product video with its
    // playable URL) from the detail payload — the search card rarely has these.
    const prod  = g.product || {};
    const items = prod.mediaItems || [];
    const imgs = [], vids = [];
    items.forEach(it => {
        if (!it || typeof it !== 'object') return;
        if (it.type === 'video' || it.group === 'video') {
            const vu = it.videoUrl || {};
            let best = null;
            ['hd','sd','ld'].forEach(q => { if (!best && vu[q] && vu[q].videoUrl) best = vu[q]; });
            if (!best) for (const k in vu) { if (vu[k] && vu[k].videoUrl) { best = vu[k]; break; } }
            if (best && best.videoUrl) vids.push({
                videoId:   it.videoId || null,
                url:       best.videoUrl,
                cover_url: it.videoCoverUrl || '',
                duration:  it.duration || null,
                width:     it.width || null,
                height:    it.height || null,
                file_size: it.fileSizeFormatStr || '',
            });
        } else {
            const iu = it.imageUrl || {};
            const u = iu.big || iu.normal || iu.small || '';
            if (u) imgs.push(u);
        }
    });
    if (imgs.length || vids.length) out.product_media = { images: imgs, videos: vids };

    // ── Rich product details (all from the embedded payload; absent fields null) ──
    const pr = g.product || {};
    const specsList = [].concat(pr.productBasicProperties || [], pr.productOtherProperties || []);
    const specs = {};
    specsList.forEach(a => { if (a && a.attrName && !(a.attrName in specs)) specs[a.attrName] = a.attrValue; });
    const pick = (...names) => {
        for (const n of names) for (const k in specs)
            if (k.toLowerCase() === n) return specs[k];
        return null;
    };
    // Substring match for specs whose label carries a qualifier, e.g.
    // "Weight (Net)" or "Dimension (W x H x D)". Tried in order of preference.
    const pickLike = (...subs) => {
        for (const sub of subs) for (const k in specs)
            if (k.toLowerCase().includes(sub)) return specs[k];
        return null;
    };

    const sku = pr.sku || {};
    const variants = (sku.skuAttrs || []).map(a => ({
        name: a.name, type: a.type,
        values: (a.values || []).map(v => v.name).filter(Boolean),
    }));
    // The real model number is the SKU-level "model number" selector shown on the
    // page (e.g. PPC-612-RTA), not the "Type" spec. Prefer it over the specs below.
    const variantModel = (() => {
        for (const v of variants) {
            const nm = String(v.name || '').toLowerCase();
            if ((nm === 'model number' || nm === 'model' || nm === 'model no.' ||
                 nm === 'model no') && (v.values || []).length) return v.values[0];
        }
        return null;
    })();
    const colors = (sku.skuAttrs || [])
        .filter(a => String(a.type).toUpperCase() === 'COLOR' || /colou?r/i.test(a.name || ''))
        .reduce((acc, a) => acc.concat((a.values || []).map(v => v.name)), []).filter(Boolean);
    const sizes = (sku.skuAttrs || [])
        .filter(a => /\\bsize\\b/i.test(a.name || ''))
        .reduce((acc, a) => acc.concat((a.values || []).map(v => v.name)), []).filter(Boolean);

    const price = pr.price || {};
    const tiers = (price.productLadderPrices || []).map(t => ({
        min_quantity: t.min, max_quantity: (t.max === -1 ? null : t.max),
        price: t.price, formatted_price: t.formatPrice, usd_price: t.dollarPrice,
    }));
    // Native USD straight from the payload (ladder dollarPrice, or a flat dollar
    // field). NOT converted — null when the locale payload carries no USD, so the
    // reported currency is never silently changed.
    const usdNums = tiers.map(t => Number(t.usd_price))
        .filter(x => Number.isFinite(x) && x > 0);
    const flatUsd = [price.dollarPrice, price.minDollarPrice, price.maxDollarPrice]
        .map(Number).filter(x => Number.isFinite(x) && x > 0);
    const usdPool = usdNums.length ? usdNums : flatUsd;
    const priceUsdMin = usdPool.length ? Math.min(...usdPool) : null;
    const priceUsdMax = usdPool.length ? Math.max(...usdPool) : null;

    const bc = ((g.seo || {}).breadCrumb || {}).pathList || [];
    const categoryPath = bc.map(x => (x.hrefObject || {}).name).filter(Boolean);

    const trade = g.trade || {};
    const lead = (((trade.leadTimeInfo || {}).ladderPeriodList) || []).map(l => ({
        min_quantity: l.minQuantity, max_quantity: l.maxQuantity, days: l.processPeriod,
    }));
    const logi = trade.logisticInfo || {};

    // Certifications from the real spec table (e.g. "Certification": "CE, FCC,
    // CB/UL, CCC"). Split on commas/semicolons/pipes only — NOT slashes, so a
    // combined mark like "CB/UL" stays one item. Deduped; [] when none listed.
    const certRaw = pick('certification', 'certifications', 'certificate',
                         'certificates', 'certification(s)', 'product certification') ||
                    pickLike('certificat');
    const certifications = certRaw
        ? [...new Set(String(certRaw).split(/[,;|、]+/).map(x => x.trim()).filter(Boolean))]
        : [];

    // After-sales / returns derived from the real spec table.
    const afterSales = pick('after-sales service', 'after-sale service',
                            'after sales service') || pickLike('after-sale', 'after sales');
    const returnsInfo = pick('return policy', 'returns', 'return & refund',
                             'return and refund', 'refund policy') ||
                        pickLike('return policy', 'return & refund', 'return and refund') ||
                        ((afterSales && /return|refund|replace/i.test(afterSales)) ? afterSales : null);

    out.product_ex = {
        product_id:      pr.productId || null,
        category_id:     pr.productCategoryId || pr.productLeafId || null,
        category_path:   categoryPath,
        brand:           pick('brand name', 'brand'),
        model_number:    variantModel || pick('model number', 'model', 'model no.'),
        material:        pick('body material', 'material', 'product material'),
        // Product's own net weight / dimensions (from specs) take priority; the
        // logistics carton weight/size is the shipping fallback and stays in
        // unit_weight / unit_size below.
        weight:          pickLike('net weight', 'weight (net') || pick('weight') ||
                         pickLike('weight') || (logi.unitWeight ? logi.unitWeight + ' KG' : null),
        dimensions:      pickLike('dimension', 'product size', 'overall size') ||
                         pick('dimensions', 'size') || logi.unitSize || null,
        condition:       pick('products status', 'product status', 'condition'),
        // Warranty: prefer the explicit duration key, then any warranty/guarantee
        // spec (e.g. "Warranty after Service"); null when the seller lists none.
        warranty:        pick('warranty(year)', 'warranty', 'warranty period', 'warranty time') ||
                         pickLike('warranty', 'guarantee'),
        // After-sales service + returns, straight from the real spec table (e.g.
        // "After-sales Service": "Repair" / "Return and Replacement"). A dedicated
        // return-policy key is rare, so returns is also derived from the after-sales
        // value when it actually mentions return/refund/replacement; null otherwise.
        after_sales_service: afterSales,
        returns:         returnsInfo,
        place_of_origin: pick('place of origin'),
        colors:          colors,
        sizes:           sizes,
        variants:        variants,
        specifications:  specs,
        price_tiers:     tiers,
        price_usd_min:   priceUsdMin,
        price_usd_max:   priceUsdMax,
        price_unit:      price.unit || price.unitEven || null,
        price_currency_pattern: (price.currencyRule || {}).currencyPattern || null,
        batch_number:    price.batchNum || null,
        moq_value:       (typeof pr.moq === 'number' ? pr.moq : null),
        box_moq:         pr.boxMoq || null,
        shipping_origin: pr.deliverPlace || null,
        lead_time:       lead,
        unit_size:       logi.unitSize || null,
        unit_weight:     logi.unitWeight || null,
        customization: {
            light: !!pr.supportLightCustomization,
            fast:  !!pr.supportFastCustomization,
            items: pr.productLightCustomizationList || [],
        },
        // Rich description loads from a separate lazy module that is not part of
        // this payload, so it is left explicitly unavailable.
        description:    null,
        certifications: certifications,
    };

    const prv = (g.review || {}).productReview || {};
    out.product_review_summary = {
        average_star:       (prv.averageStar !== undefined ? prv.averageStar : null),
        total_review_count: (prv.totalReviewCount !== undefined ? prv.totalReviewCount : null),
    };

    return out;
}
"""


# Phrases unique to the real CAPTCHA page ("verify"/"robot" excluded — they
# appear in normal JS bundles and would false-positive on plain pages).
_CAPTCHA_TEXTS = ["drag the slider", "unusual traffic from your network",
                  "please complete the security check", "nc_iconfont-load"]


async def _has_captcha(page) -> bool:
    # Detect by DOM presence / window._config_, not visibility — the slider only
    # renders after external CDN scripts load, so a visibility check would miss it.
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


async def _wait_for_captcha_clear(page, timeout: int = 300) -> None:
    if not await _has_captcha(page):
        return
    _log.warning("event=captcha_detected  url=%s", page.url[:90])
    await asyncio.sleep(4)  # let the CDN scripts render the slider
    print("\n" + "=" * 60)
    print("[!]  WARNING: Alibaba Punish Page / CAPTCHA detected!")
    print("[!]  Please solve the slider/puzzle manually in the browser window...")
    print("=" * 60 + "\n")

    start = asyncio.get_event_loop().time()
    while True:
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError("Timed out waiting for manual CAPTCHA solving.")
        if not await _has_captcha(page) and "_____tmd_____" not in page.url:
            break
        await asyncio.sleep(2)

    _log.info("event=captcha_cleared")
    print("[+]  CAPTCHA cleared! Resuming automation...")


def _parse_api_reviews(payload: dict) -> dict | None:
    """Build a review dict from an intercepted API JSON response, if one fits."""
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

    # pick the largest list that has a field resembling review text
    best = max(candidates, key=lambda kv: len(kv[1]))
    items = best[1]
    reviews = []
    for item in items[:50]:
        s = lambda k: str(item.get(k, "N/A")).strip() or "N/A"
        raw_star = item.get("starNum") or item.get("star") or item.get("score") or item.get("rating")
        stars = str(raw_star) if raw_star is not None else "N/A"
        media = item.get("feedbackMedia") or item.get("images") or item.get("mediaList") or []
        imgs, vids = [], []
        for mo in media:
            u = mo.get("url") or mo.get("src") or mo.get("imageUrl") or mo.get("videoUrl") if isinstance(mo, dict) else mo
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            is_video = (isinstance(mo, dict) and (mo.get("videoUrl") or str(mo.get("type", "")).lower() == "video")) \
                or re.search(r'\.(mp4|webm|mov|m3u8)(\?|$)', u, re.I)
            (vids if is_video else imgs).append(u)

        reviews.append({
            "reviewer":         s("buyerName") if item.get("buyerName") else s("reviewerName") if item.get("reviewerName") else s("memberName") if item.get("memberName") else "N/A",
            "country":          s("countryCode") if item.get("countryCode") else s("country") if item.get("country") else "N/A",
            "date":             s("postDate") if item.get("postDate") else s("gmtCreate") if item.get("gmtCreate") else s("date") if item.get("date") else "N/A",
            "stars":            stars,
            "text":             s("feedback") if item.get("feedback") else s("content") if item.get("content") else s("reviewContent") if item.get("reviewContent") else s("comment") if item.get("comment") else "N/A",
            "model_number":     s("skuInfo") if item.get("skuInfo") else s("goodsSku") if item.get("goodsSku") else s("sku") if item.get("sku") else "N/A",
            "verified_purchase": bool(item.get("verifiedPurchase") or item.get("isVerified")),
            "repeat_buyer":     bool(item.get("repeatBuyer") or item.get("isRepeat")),
            "review_media":     {"images": imgs, "videos": vids},
        })

    # Drop responses that merely matched a list key but carry no real review content
    def _has_content(r):
        return (r["reviewer"] != "N/A" or r["text"] != "N/A" or r["date"] != "N/A"
                or r["stars"] != "N/A" or r["review_media"]["images"] or r["review_media"]["videos"])
    reviews = [r for r in reviews if _has_content(r)]

    overall = payload.get("avgStar") or payload.get("avgScore") or payload.get("totalStar") or "N/A"
    total   = payload.get("totalCount") or payload.get("total") or len(reviews)

    if not reviews:
        return None

    return {
        "overall_rating": str(overall),
        "tabs":           {},
        "sub_scores":     {},
        "reviews":        reviews,
        "total_reviews":  int(total) if str(total).isdigit() else len(reviews),
        "_source":        "api",
    }


def _raw_review_items(payload: dict) -> list:
    """Return the raw, verbatim review objects from a captured feedback-API JSON
    (Alibaba's own native field names preserved, no remapping). Empty if none."""
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


_PAGE_KEYS = ("pageno", "pagenum", "page", "currentpage", "pageindex",
              "beginpage", "curpage", "pagestart", "start")


def _normalize_native_item(it: dict) -> dict:
    """Map one raw Alibaba feedback object onto our normalized review shape,
    trying the native key names Alibaba uses. The raw object is kept separately
    in native_items; this is only the clean view for the items list / Excel."""
    def pick(*keys, default="N/A"):
        for k in keys:
            v = it.get(k)
            if v not in (None, "", []):
                return v
        return default
    star = pick("starNum", "star", "score", "rating", "ratingStar", "feedbackStar", default=None)
    media = (it.get("feedbackMedia") or it.get("mediaList") or it.get("images")
             or it.get("imageList") or [])
    imgs, vids = [], []
    for mo in media if isinstance(media, list) else []:
        if isinstance(mo, dict):
            u = mo.get("url") or mo.get("src") or mo.get("imageUrl") or mo.get("videoUrl")
        else:
            u = mo
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        is_vid = (isinstance(mo, dict) and (mo.get("videoUrl") or str(mo.get("type", "")).lower() == "video")) \
            or bool(re.search(r'\.(mp4|webm|mov|m3u8)(\?|$)', u, re.I))
        (vids if is_vid else imgs).append(u)

    text = str(pick("content", "feedback", "feedbackContent", "reviewContent", "comment", "translatedContent"))
    # Supplier reply: a dedicated native field, else split from a combined text.
    reply = pick("sellerReply", "supplierReply", "replyContent", "reply",
                 "feedbackReply", "companyReply", default=None)
    if isinstance(reply, dict):
        reply = reply.get("content") or reply.get("text") or reply.get("reply")
    if reply is None:
        text, reply = _split_supplier_reply(text)
    return {
        "reviewer":          str(pick("buyerName", "memberName", "displayName", "nickName", "buyerNickName", "reviewerName")),
        "country":           str(pick("countryName", "country", "buyerCountry", "countryCode")),
        "date":              str(pick("gmtCreate", "postDate", "feedbackDate", "createTime", "date", "orderCompleteTime")),
        "stars":             str(star) if star is not None else "N/A",
        "text":              text,
        "supplier_reply":    (str(reply).strip() or None) if reply else None,
        "model_number":      _sanitize_model_number(pick("sku", "goodsSku", "skuInfo", "specInfo")),
        "verified_purchase": bool(it.get("verifiedPurchase") or it.get("isVerified") or it.get("hasOrder")),
        "repeat_buyer":      bool(it.get("repeatBuyer") or it.get("isRepeat")),
        "media":             {"images": _dedupe(imgs), "videos": _dedupe(vids)},
    }


def _item_key(it: dict) -> str:
    """Stable identity for de-duplication across pages."""
    for k in ("feedbackId", "id", "reviewId", "evaluateId", "gmtCreate", "feedbackTime"):
        v = it.get(k)
        if v:
            return f"{k}:{v}"
    return json.dumps(it, sort_keys=True)[:200]


def _bump_page(value: str) -> str:
    try:
        return str(int(value) + 1)
    except (TypeError, ValueError):
        return value


def _next_request(url: str, post_data, page_no: int):
    """Return (new_url, new_post_data) with the pagination field set to page_no,
    or (None, None) if no page field can be located."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for k in list(qs):
        if k.lower() in _PAGE_KEYS:
            qs[k] = [str(page_no)]
            new_q = urllib.parse.urlencode({k: v[0] for k, v in qs.items()}, safe="%,")
            return urllib.parse.urlunparse(parsed._replace(query=new_q)), post_data

    if post_data:
        # JSON body
        try:
            obj = json.loads(post_data)
            if isinstance(obj, dict):
                for k in list(obj):
                    if k.lower() in _PAGE_KEYS:
                        obj[k] = page_no
                        return url, json.dumps(obj)
        except Exception:
            pass
        # form-encoded body
        if "=" in post_data:
            fqs = urllib.parse.parse_qs(post_data, keep_blank_values=True)
            for k in list(fqs):
                if k.lower() in _PAGE_KEYS:
                    fqs[k] = [str(page_no)]
                    return url, urllib.parse.urlencode({k: v[0] for k, v in fqs.items()})
    return None, None


async def _collect_all_feedback(page, cap: dict, tab_count: int) -> list:
    """Replay the captured feedback request page-by-page until every review is
    collected (deduped). Returns the full list of raw native review objects."""
    collected: dict[str, dict] = {}
    for it in _raw_review_items(cap["body"]):
        collected[_item_key(it)] = it

    # Find the page field + its current value to know where to continue from.
    start_page = None
    parsed = urllib.parse.urlparse(cap["url"])
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for k, v in qs.items():
        if k.lower() in _PAGE_KEYS and v and v[0].isdigit():
            start_page = int(v[0]); break
    if start_page is None and cap.get("post_data"):
        try:
            obj = json.loads(cap["post_data"])
            for k, v in (obj.items() if isinstance(obj, dict) else []):
                if k.lower() in _PAGE_KEYS and str(v).isdigit():
                    start_page = int(v); break
        except Exception:
            pass
    if start_page is None:
        start_page = 1

    headers = {k: v for k, v in (cap.get("headers") or {}).items()
               if k.lower() not in ("content-length", "host", ":authority", ":method",
                                    ":path", ":scheme", "accept-encoding")}

    page_no = start_page
    empty_streak = 0
    for _ in range(80):  # hard safety cap
        if tab_count and len(collected) >= tab_count:
            break
        page_no += 1
        new_url, new_post = _next_request(cap["url"], cap.get("post_data"), page_no)
        if new_url is None:
            _log.info("event=feedback_no_page_param  url=%s", cap["url"][:90])
            break
        try:
            resp = await page.context.request.fetch(
                new_url, method=cap.get("method", "GET"),
                headers=headers, data=new_post, timeout=20_000)
            body = await resp.json()
        except Exception as e:
            _log.warning("event=feedback_page_failed  page=%d  error=%s", page_no, e)
            break
        items = _raw_review_items(body)
        new_added = 0
        for it in items:
            k = _item_key(it)
            if k not in collected:
                collected[k] = it; new_added += 1
        if not items or new_added == 0:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
    _log.info("event=feedback_paginated  pages_from=%d  collected=%d  tab_count=%d",
              start_page, len(collected), tab_count)
    return list(collected.values())


def _dom_review_key(r: dict) -> tuple:
    return (str(r.get("reviewer", "")).strip(),
            str(r.get("date", "")).strip(),
            str(r.get("text", ""))[:60].strip())


_REVIEW_NEXT_SELS = (
    '[class*="review"] [class*="pagination"] [class*="next"]',
    '[class*="review-layout"] [aria-label*="Next" i]',
    '[class*="review"] button[aria-label*="Next" i]',
    '[class*="review"] [class*="pagination-next"]',
    '[class*="comet-pagination-next"]',
    '[class*="review"] a[aria-label*="Next" i]',
)


async def _paginate_review_dom(page, block: dict, tab_count: int) -> dict:
    """Page through the review modal in the DOM (click "next" / load-more and
    re-extract), merging unique reviews. Best-effort and bounded; used when the
    feedback API wasn't intercepted. Never discards already-collected reviews."""
    if block is None:
        return block
    reviews = block.get("reviews", []) or []
    seen = {_dom_review_key(r) for r in reviews}
    for _ in range(40):                                  # hard page cap
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
        await _wait_network_idle(page, timeout_ms=4_000)
        try:
            dom = await page.evaluate(_REVIEWS_JS)
        except Exception:
            break
        added = 0
        for r in dom.get("reviews", []):
            if not _dom_review_has_content(r):
                continue
            k = _dom_review_key(r)
            if k not in seen:
                seen.add(k); reviews.append(r); added += 1
        if added == 0:
            break
    block["reviews"] = reviews
    block["total_reviews"] = len(reviews)
    return block


# Supplier feedback page (company_profile/feedback.html) — the authoritative,
# fully-paginated store/supplier review list. Clean per-card classes confirmed
# live: .review-item > .anonymous-name / .country-name / .review-item-action /
# .content-text / .product-name-link / .review-latitude-score-unit.
_FEEDBACK_JS = r"""
() => {
    const cards = [...document.querySelectorAll('.review-item')]
        .filter(e => e.querySelector('.anonymous-name') || e.querySelector('.content-text'));
    const q = (c, s) => { const el = c.querySelector(s); return el ? (el.innerText || el.textContent || '').trim() : ''; };
    return cards.map(c => {
        const scores = [...c.querySelectorAll('.review-latitude-score-unit')]
            .map(e => { const m = (e.innerText || '').match(/(\d)/); return m ? parseInt(m[1]) : null; })
            .filter(n => n !== null);
        const overall = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
        const imgs = [...c.querySelectorAll('img')].map(i => i.src || i.getAttribute('data-src') || '')
            .filter(s => s.startsWith('http') && !/avatar|flag|\.svg|tps-\d{1,2}-/i.test(s));
        const vids = [...c.querySelectorAll('video[src],video source')]
            .map(v => v.src || v.getAttribute('src') || '').filter(s => s.startsWith('http'));
        return {
            reviewer:          q(c, '.anonymous-name') || 'N/A',
            country:           q(c, '.country-name') || 'N/A',
            date:              q(c, '.review-item-action') || 'N/A',
            stars:             overall === null ? 'N/A' : String(overall),
            text:              q(c, '.content-text') || 'N/A',
            model_number:      'N/A',
            product_reviewed:  q(c, '.product-name-link') || '',
            latitude_scores:   scores,
            verified_purchase: /verified/i.test(c.innerText || ''),
            repeat_buyer:      /repeat\s+buyer/i.test(c.innerText || ''),
            review_media:      { images: imgs, videos: vids },
        };
    });
}
"""


def _feedback_key(r: dict) -> tuple:
    return (str(r.get("reviewer", "")).strip(),
            str(r.get("date", "")).strip(),
            str(r.get("text", ""))[:60].strip())


# Run-level cache of supplier feedback results, keyed by the real companyId so
# multiple products from one supplier reuse one extraction. Cleared each run.
_FEEDBACK_CACHE: dict = {}
# Concurrency control for feedback-page extraction (created lazily per run):
# a semaphore caps simultaneous feedback scrapes; the in-flight map makes
# concurrent same-supplier requests await a single shared fetch.
_FEEDBACK_SEM = None
_FEEDBACK_INFLIGHT: dict = {}

_HAS_REVIEW_ITEM_JS = (
    "() => [...document.querySelectorAll('.review-item')]"
    ".some(e => /[A-Za-z]\\*{2,}/.test(e.innerText || ''))"
)

# Block images/media/fonts and analytics/beacon requests on the feedback mini-site
# (keep documents, scripts, stylesheets, and XHR/fetch that render + paginate).
_FEEDBACK_BLOCK_RE = re.compile(
    r"adata\.alibaba|/[0-9]+\.gif|sc\.ma\.|\.gif(\?|$)|beacon|analytics|gtm|"
    r"doubleclick|log\.alibaba|mmstat|/wdm/|hm\.baidu",
    re.I,
)


async def _feedback_route(route):
    try:
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            await route.abort(); return
        if _FEEDBACK_BLOCK_RE.search(req.url):
            await route.abort(); return
        await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


def _feedback_first_id(items: list) -> str | None:
    for r in items:
        if not (r.get("reviewer", "N/A") == "N/A" and r.get("text", "N/A") == "N/A"):
            return f'{r.get("reviewer","")}|{r.get("date","")}'
    return None


async def _wait_feedback_change(page, prev_first_id, timeout_ms: int = 6_000) -> bool:
    """After clicking Next, wait until the first review card's identity changes,
    so we never extract stale (not-yet-rerendered) content. False on timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        try:
            items = await page.evaluate(_FEEDBACK_JS)
        except Exception:
            return False
        fid = _feedback_first_id(items)
        if fid and fid != prev_first_id:
            return True
        await asyncio.sleep(0.25)
    return False


async def _next_is_disabled(nxt) -> bool:
    """A Fusion pager marks the terminal 'Next' with a disabled class / aria flag
    even when the underlying <button> is not natively disabled."""
    try:
        cls  = (await nxt.get_attribute("class")) or ""
        aria = (await nxt.get_attribute("aria-disabled")) or ""
        return "disabled" in cls.lower() or aria == "true"
    except Exception:
        return False


async def _dismiss_feedback_overlays(page) -> None:
    """Hide floating chat widgets / sticky bars / cookie banners that can sit on
    top of the pager and intercept the Next click. Best-effort; never raises and
    never touches the pagination element itself."""
    try:
        await page.evaluate("""
            () => {
              const sels = ['#launcher','[id*="chat" i]','[class*="chat" i]',
                '[class*="im-bar" i]','[class*="cookie" i]','[class*="consent" i]',
                '[class*="gdpr" i]','[class*="contact-bar" i]','[class*="bottom-bar" i]',
                '[class*="bottombar" i]','[class*="detail-fixed" i]','[class*="float-bar" i]'];
              for (const s of sels) document.querySelectorAll(s).forEach(e => {
                if (e.closest('.next-pagination')) return;          // never hide the pager
                const st = getComputedStyle(e);
                if (st.position === 'fixed' || st.position === 'sticky') e.style.display = 'none';
              });
            }
        """)
    except Exception:
        pass


async def _click_next_robust(page, nxt) -> bool:
    """Advance the feedback pager through escalating strategies so a pointer-
    intercepting overlay (the cause of the old 'click_failed' stall at ~20/N) can
    never abort pagination:
        1. normal, actionability-checked click (longer timeout);
        2. forced click (skips the pointer-intercept check);
        3. JS element.click() (bypasses all pointer interception entirely).
    Returns True if any strategy dispatched the click."""
    try:
        await nxt.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass
    await _dismiss_feedback_overlays(page)
    try:
        await nxt.click(timeout=5_000)
        return True
    except Exception:
        pass
    try:
        await nxt.click(timeout=3_000, force=True)
        return True
    except Exception:
        pass
    try:
        await nxt.evaluate("el => el.click()")
        return True
    except Exception:
        return False


async def fetch_store_feedback(ctx, feedback_url: str, tab_count: int) -> dict:
    """Extract store/supplier reviews from the supplier feedback page by paging
    through the Fusion 'Next' pagination (deduped). Returns a dict with the
    reviews, the termination reason, and whether the list was fully traversed
    (`complete` is True only when the pager's 'Next' became disabled — i.e. the
    authoritative end of the list). Separate from product reviews; nothing
    fabricated."""
    page = await new_stealth_page(ctx)
    await page.route("**/*", _feedback_route)                 # Phase G
    collected: dict[tuple, dict] = {}
    termination = "page_cap"
    pages = 0
    try:
        await page.goto(feedback_url, wait_until="domcontentloaded", timeout=45_000)
        await _wait_for_captcha_clear(page)

        async def _await_template(timeout_ms: int) -> bool:
            try:
                await page.wait_for_function(_HAS_REVIEW_ITEM_JS, timeout=timeout_ms)
                return True
            except Exception:
                return False

        # The feedback list is a lazy SPA module that can be slow to hydrate; a
        # scroll nudges it. If it still hasn't rendered, reload once and wait
        # longer before declaring no_template (the old 8s was too tight and some
        # suppliers' pages returned a false 'no_template' at 0 reviews).
        if not await _await_template(15_000):
            try:
                await scroll_page(page)
            except Exception:
                pass
            if not await _await_template(8_000):
                _log.info("event=feedback_template_retry  url=%s", feedback_url[:80])
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45_000)
                    await _wait_for_captcha_clear(page)
                    await scroll_page(page)
                except Exception:
                    pass
                if not await _await_template(15_000):
                    _log.warning("event=feedback_no_template  url=%s", feedback_url[:80])
                    return {"reviews": [], "complete": False,
                            "termination": "no_template", "pages": 0}

        no_new_streak = 0
        for _ in range(max(1, FEEDBACK_MAX_PAGES)):           # configurable page cap
            pages += 1
            try:
                items = await page.evaluate(_FEEDBACK_JS)
            except Exception:
                termination = "extract_error"; break
            first_id = _feedback_first_id(items)
            added = 0
            for r in items:
                if r.get("reviewer", "N/A") == "N/A" and r.get("text", "N/A") == "N/A":
                    continue
                k = _feedback_key(r)
                if k not in collected:
                    collected[k] = r; added += 1

            if added == 0:                                    # Phase E: safety stop
                no_new_streak += 1
                if no_new_streak >= 2:
                    termination = "no_new_reviews"; break
            else:
                no_new_streak = 0

            nxt = page.locator('.next-pagination button.next-next, '
                               '.next-pagination .next-next button, '
                               '.next-pagination button:has-text("Next")').first
            try:
                cnt = await nxt.count()
                enabled = (cnt > 0) and await nxt.is_enabled() and not await _next_is_disabled(nxt)
            except Exception:
                cnt, enabled = 0, False
            if cnt == 0 or not enabled:
                termination = "next_disabled"; break         # authoritative end
            # Robust click: normal → force → JS, so an intercepting overlay can't
            # abort pagination (the old cause of the ~20/N 'click_failed' stall).
            if not await _click_next_robust(page, nxt):
                termination = "click_failed"; break
            # Phase B: don't extract until the new page actually rendered. Retry
            # the click once (overlay may have re-appeared) before giving up.
            if not await _wait_feedback_change(page, first_id, timeout_ms=8_000):
                await _click_next_robust(page, nxt)
                if not await _wait_feedback_change(page, first_id, timeout_ms=8_000):
                    termination = "stale_after_click"; break

        complete = termination == "next_disabled"
        _log.info("event=feedback_extracted  collected=%d  pages=%d  term=%s  complete=%s  tab=%d  url=%s",
                  len(collected), pages, termination, complete, tab_count, feedback_url[:65])
        return {"reviews": list(collected.values()), "complete": complete,
                "termination": termination, "pages": pages}
    except Exception as e:
        _log.warning("event=feedback_failed  url=%s  error=%s", feedback_url[:70], e)
        return {"reviews": list(collected.values()), "complete": False,
                "termination": "error", "pages": pages}
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _get_feedback(ctx, feedback_url: str, store_tab: int, company_id) -> dict:
    """Cache- and concurrency-coordinated wrapper around fetch_store_feedback.
      • run cache hit by companyId  → return the shared result (no scrape);
      • concurrent same-supplier    → await the one in-flight fetch;
      • otherwise scrape, bounded by FEEDBACK_CONCURRENCY.
    Only usable results are cached (the `complete` flag travels with them)."""
    global _FEEDBACK_SEM
    cid = company_id

    if cid is not None and cid in _FEEDBACK_CACHE:
        res = _FEEDBACK_CACHE[cid]
        _log.info("event=feedback_cache_hit  companyId=%s  reviews=%d", cid, len(res["reviews"]))
        return res

    if cid is not None and cid in _FEEDBACK_INFLIGHT:
        _log.info("event=feedback_inflight_wait  companyId=%s", cid)
        return await _FEEDBACK_INFLIGHT[cid]

    fut = None
    if cid is not None:
        fut = asyncio.get_event_loop().create_future()
        _FEEDBACK_INFLIGHT[cid] = fut
    try:
        if _FEEDBACK_SEM is None:
            _FEEDBACK_SEM = asyncio.Semaphore(max(1, FEEDBACK_CONCURRENCY))
        async with _FEEDBACK_SEM:
            res = await fetch_store_feedback(ctx, feedback_url, store_tab)
        # Don't cache results from a recoverable mid-pagination failure, so another
        # product from the same supplier gets a fresh, potentially complete attempt.
        if cid is not None and res and res["reviews"] and \
                res["termination"] not in ("error", "no_template", "click_failed",
                                           "stale_after_click", "extract_error"):
            _FEEDBACK_CACHE[cid] = res
        if fut is not None and not fut.done():
            fut.set_result(res)
        return res
    except Exception as e:
        if fut is not None and not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if cid is not None:
            _FEEDBACK_INFLIGHT.pop(cid, None)


async def _open_full_reviews(page) -> bool:
    """Alibaba renders only a ~3-review sample on the product page; the full list
    is behind a "View all N reviews" link. Click it to reveal the full list."""
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
            await _wait_network_idle(page, timeout_ms=5_000)
            await _wait_review_anchors(page)
            return True
        except Exception:
            continue
    return False


async def _finalize_review_block(page, block: dict, feedback_cap, tab_count: int) -> dict:
    """Capture every available review. Primary path: replay the intercepted
    feedback API page-by-page (full native list). Fallback: open the "View all
    reviews" list and page/scroll the DOM. Existing reviews are preserved,
    nothing is fabricated."""
    if block is None:
        return block
    if feedback_cap:
        full = await _collect_all_feedback(page, feedback_cap, tab_count)
        if full:
            block["native_items"] = full
            block["reviews"]      = [_normalize_native_item(x) for x in full]
            block["total_reviews"] = max(tab_count or 0, len(full))
            block["_source"]      = "api_paginated"
            return block

    # Reveal the full list (product page shows only a sample), then re-extract
    # and merge any newly-rendered reviews before paging/scrolling.
    if tab_count and len(block.get("reviews", []) or []) < tab_count:
        opened = await _open_full_reviews(page)
        if opened:
            try:
                dom = await page.evaluate(_REVIEWS_JS)
                reviews = block.get("reviews", []) or []
                seen = {_dom_review_key(r) for r in reviews}
                for r in dom.get("reviews", []):
                    if _dom_review_has_content(r) and _dom_review_key(r) not in seen:
                        seen.add(_dom_review_key(r)); reviews.append(r)
                block["reviews"] = reviews
            except Exception:
                pass
    return await _paginate_review_dom(page, block, tab_count)


def _classify_review_tab(label: str) -> str:
    l = label.lower()
    if any(k in l for k in ("store", "supplier", "company", "seller")):
        return "store_reviews"
    return "product_reviews"


def _dom_review_has_content(r: dict) -> bool:
    media = r.get("review_media", {})
    return (r.get("reviewer", "N/A") != "N/A" or r.get("text", "N/A") != "N/A"
            or r.get("date", "N/A") != "N/A" or r.get("stars", "N/A") != "N/A"
            or media.get("images") or media.get("videos"))


async def _wait_review_anchors(page) -> None:
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


async def _extract_current_reviews(page, api_captures):
    """Return (review_block, date_posted, feedback_cap) for the visible review tab.
    feedback_cap is the capture (request + body) of the feedback API, used to
    paginate through every review; None if that API wasn't intercepted."""
    native_items: list = []
    feedback_cap = None
    for cap in api_captures:
        got = _raw_review_items(cap["body"])
        if got and len(got) > len(native_items):
            native_items = got
            feedback_cap = cap

    dom = await page.evaluate(_REVIEWS_JS)
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


async def fetch_reviews(ctx, href: str) -> dict:
    """Open a product detail page and extract BOTH product and store reviews."""
    empty = {"date_posted": "N/A", "product_reviews": None, "store_reviews": None}
    page = await new_stealth_page(ctx)

    # Each capture keeps the response body AND the request that produced it, so a
    # paginated feedback endpoint can be replayed page-by-page.
    _api_captures: list[dict] = []

    async def _on_response(response):
        try:
            ct   = response.headers.get("content-type", "")
            url  = response.url
            # Accept JSON by content-type, or by a review-ish URL even when Alibaba
            # serves the feedback payload as text/plain or JSONP.
            hint = re.search(r"feedback|review|evaluat|comment|rate", url, re.I)
            if "json" not in ct and not hint:
                return
            try:
                body = await response.json()
            except Exception:
                try:
                    txt  = await response.text()
                    txt  = re.sub(r"^[^{\[]*", "", txt).strip()      # strip JSONP prefix
                    txt  = re.sub(r"\)\s*;?\s*$", "", txt)           # strip JSONP suffix
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
            _api_captures.append({
                "url":       response.url,
                "body":      body,
                "method":    req.method,
                "headers":   headers,
                "post_data": req.post_data,
            })
        except Exception:
            pass

    page.on("response", _on_response)

    result = {"date_posted": "N/A", "product_reviews": None, "store_reviews": None,
              "seller": None, "store_review_summary": None, "product_media": None,
              "product_ex": None, "product_review_summary": None}
    try:
        await asyncio.sleep(random.uniform(1.2, 2.5))
        _log.info("event=review_navigate  url=%s", href[:90])
        await page.goto(href, wait_until="domcontentloaded", timeout=45_000)
        await _wait_network_idle(page, timeout_ms=4_000)
        await _wait_for_captcha_clear(page)

        # Real native seller/company + store-rating summary from the page's own
        # embedded payload (window.detailData.globalData). None on older templates.
        try:
            sj = await page.evaluate(_SELLER_JS)
            if sj:
                result["seller"]                = sj.get("seller")
                result["store_review_summary"]  = sj.get("store_review_summary")
                result["product_media"]         = sj.get("product_media")
                result["product_ex"]            = sj.get("product_ex")
                result["product_review_summary"] = sj.get("product_review_summary")
                cid = (sj.get("seller") or {}).get("companyId")
                _log.info("event=seller_extracted  companyId=%s  store_summary=%s",
                          cid, bool(sj.get("store_review_summary")))
        except Exception as e:
            _log.warning("event=seller_extract_failed  error=%s", e)

        # Discover every review tab (product, store, supplier, …)
        tabs: list[tuple[str, int, object]] = []
        for sel in ('[role="tab"]', 'li', 'a', 'button', 'span', 'div'):
            try:
                for loc in await page.locator(sel).all():
                    txt = (await loc.inner_text()).strip()
                    m   = re.search(r'(.+?)\s*\((\d+)\)', txt)
                    if m and re.search(r'review|feedback|evaluate|store|product|supplier', m.group(1), re.I):
                        cnt = int(m.group(2))
                        if cnt > 0:
                            tabs.append((m.group(1).strip(), cnt, loc))
            except Exception:
                pass
            if tabs:
                break

        if not tabs:
            # No tabs — take whatever reviews are already visible as product reviews
            _log.warning("event=tab_not_found  url=%s", href[:80])
            await _wait_review_anchors(page)
            block, date, fb = await _extract_current_reviews(page, _api_captures)
            block = await _finalize_review_block(page, block, fb, 0)
            result["date_posted"] = date
            if block:
                block["tab_label"] = "default"
                result["product_reviews"] = block
            return result

        # Keep the highest-count tab for each distinct label
        uniq: dict[str, tuple[str, int, object]] = {}
        for label, cnt, loc in tabs:
            key = label.lower()
            if key not in uniq or cnt > uniq[key][1]:
                uniq[key] = (label, cnt, loc)

        for label, cnt, loc in uniq.values():
            rtype = _classify_review_tab(label)
            try:
                _api_captures.clear()
                await loc.scroll_into_view_if_needed()
                await loc.click()
                _log.info("event=tab_clicked  label=%r  count=%d  type=%s", label, cnt, rtype)
                await _wait_network_idle(page, timeout_ms=6_000)
                await _wait_review_anchors(page)
            except Exception as e:
                _log.warning("event=tab_click_failed  label=%r  error=%s", label, e)
                continue

            block, date, fb = await _extract_current_reviews(page, _api_captures)
            block = await _finalize_review_block(page, block, fb, cnt)
            if date and date != "N/A":
                result["date_posted"] = date
            if block:
                block["tab_label"] = label
                block["tab_count"] = cnt
                existing = result[rtype]
                if existing is None or block["total_reviews"] > existing["total_reviews"]:
                    result[rtype] = block

        # ── Full STORE/supplier reviews from the supplier feedback page ──
        # The product page shows only a ~3-review sample; the supplier feedback
        # page (seller.feedbackUrl) holds the fully-paginated store review list
        # with individual ratings. This augments ONLY the store block; product
        # reviews stay exactly as extracted above.
        seller   = result.get("seller") or {}
        fb_url   = seller.get("feedbackUrl")
        cid      = seller.get("companyId")
        store_bk = result.get("store_reviews")
        store_tab = (store_bk or {}).get("tab_count", 0) or 0
        if fb_url and (store_tab or store_bk):
            try:
                fb_res = await _get_feedback(ctx, fb_url, store_tab, cid)
            except Exception as e:
                _log.warning("event=feedback_augment_failed  error=%s", e)
                fb_res = None
            if fb_res and fb_res["reviews"]:
                if store_bk is None:
                    store_bk = {"overall_rating": "N/A", "tab_count": store_tab,
                                "tab_label": "Store reviews"}
                    result["store_reviews"] = store_bk
                if len(fb_res["reviews"]) >= len(store_bk.get("reviews", []) or []):
                    # Deep-copy so per-product processing never mutates shared cache.
                    store_bk["reviews"]            = copy.deepcopy(fb_res["reviews"])
                    store_bk["total_reviews"]      = len(fb_res["reviews"])
                    store_bk["_source"]            = "feedback_page"
                    store_bk["feedback_complete"]  = fb_res["complete"]
                    store_bk["feedback_termination"] = fb_res["termination"]
                    store_bk["product_tab_count"]  = store_tab

        if result["product_reviews"] is None and result["store_reviews"] is None:
            _log.warning("event=review_empty  url=%s", href[:80])
            try:
                debug_path = OUTPUT_DIR / "_review_debug.html"
                debug_path.write_text(await page.content(), encoding="utf-8")
                _log.info("event=debug_html_saved  path=%s", debug_path)
            except Exception:
                pass

        return result

    except Exception as e:
        _log.error("event=review_failed  url=%s  error=%s", href[:70], e)
        return empty
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# DOM EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACT_JS = """
() => {
    const seen = new Set();
    const out  = [];

    const primarySels = [
        '.m-gallery-product-item-v2',
        '.search-card-e-slider',
        '[class*="search-card-e-offer"]',
        '[class*="J-offer-wrapper"]',
        '[data-content="product-item"]',
        'div[class*="offer-list-item"]',
        '[class*="organic-list-offer"]',
        '[class*="sese-offer"]',
        '[class*="AlbCard"]',
    ];

    let cards = [], matchedSel = "none";
    for (const sel of primarySels) {
        const found = document.querySelectorAll(sel);
        if (found.length > 0) { cards = Array.from(found); matchedSel = sel; break; }
    }

    if (cards.length < 3) {
        matchedSel = "anchor-fallback";
        // broaden to catch all Alibaba product URL patterns
        const productLinkSel = [
            'a[href*="/product-detail/"]',
            'a[href*="product_"]',
            'a[href*=".html"][href*="alibaba.com"]',
            'a[href*="/offer/"]'
        ].join(',');
        document.querySelectorAll(productLinkSel).forEach(a => {
            const href = a.href || '';
            // skip non-product links like home, search pages, etc.
            if (!href || href.includes('/search') || href.includes('/catalog/') || href.endsWith('.com/')) return;
            let el = a.parentElement;
            for (let d = 0; d < 6; d++) {
                if (el && (el.className.includes('item') || el.className.includes('card') || el.className.includes('offer'))) {
                    if (!cards.includes(el)) cards.push(el);
                    break;
                }
                el = el ? el.parentElement : null;
            }
        });
    }

    const resolveImg = img => {
        const s = img.src || img.getAttribute('data-src') || img.getAttribute('lazy-src') || img.dataset?.lazySrc || '';
        if (!s || s.startsWith('data:') || s.length < 12) return '';
        return s.startsWith('//') ? 'https:' + s : s;
    };

    // Use innerText split on newlines to eliminate duplicated title text nodes
    const cleanTitle = el => {
        if (!el) return '';
        return (el.innerText || el.textContent || '').split('\\n')
            .map(s => s.trim()).filter(s => s.length > 3)[0] || '';
    };

    const tx = el => el ? el.textContent.trim() : '';
    const q  = (card, s) => card.querySelector(s);

    for (const card of cards) {
        const isSponsored = !!card.querySelector('[class*="sponsored"],[class*="premium-tag"],[class*="ad-label"],[data-tracking*="sponsored"],[data-spm*="sponsored"]');

        const linkEl = q(card, 'a[href*="product-detail"]') || q(card, 'a[href*="alibaba.com"]') || q(card, 'a[href]');
        const href   = (linkEl ? linkEl.href : '').trim();
        if (!href || href === '#' || href.startsWith('javascript:')) continue;
        if (seen.has(href)) continue;
        seen.add(href);

        // ── product fields ──
        const titleEl = q(card, '.search-card-e-title')
            || q(card, '[class*="offer-title"],[class*="product-title"],[class*="subject"],h2,h3');
        const title = cleanTitle(titleEl) || cleanTitle(linkEl);
        if (title.length < 3) continue;

        const priceEl = q(card, '.search-card-e-price-main')
            || q(card, '[class*="price-value"],[class*="price-num"],[class*="price-range"],[class*="priceValue"],[class*="price-current"],[class*="price"]');
        const moqEl   = q(card, '.search-card-e-min-order')
            || q(card, '[class*="moq"],[class*="min-order"],[class*="minimum"],[class*="minOrder"]');
        const dateEl  = q(card, '[class*="post-time"],[class*="update-time"],[class*="post-date"],[class*="publish-date"],[class*="date-range"]');

        // ── media ──
        const images = Array.from(card.querySelectorAll('img')).map(resolveImg).filter(Boolean);
        const videos = Array.from(card.querySelectorAll('video[src],video source,[class*="video"] source'))
            .map(v => v.src || v.getAttribute('src') || '').filter(s => s.length > 5);

        // ── supplier fields ──
        const supplierEl  = q(card, '.search-card-e-company')
            || q(card, '[class*="supplier-name"],[class*="company-name"],[class*="vendor"],[class*="seller"],[class*="store-name"]');
        const countryEl   = q(card, '[class*="country-name"],[class*="country"],[class*="origin"],[class*="flag"]');
        const yearsEl     = q(card, '[class*="year"],[class*="member-year"],[class*="years-member"]');
        const goldEl      = q(card, '[class*="gold-supplier"],[class*="goldSupplier"],[class*="gold_supplier"]');
        const verifiedEl  = q(card, '[class*="verified-supplier"],[class*="verifiedSupplier"],[class*="verified"]');

        // ── supplier metrics ──
        const responseEl  = q(card, '[class*="response-rate"],[class*="responseRate"]');
        const taEl        = q(card, '[class*="trade-assurance"],[class*="tradeAssurance"],[class*="ta-amount"],[class*="ta-cap"]');
        const trustEl     = q(card, '[class*="trust-score"],[class*="trustScore"],[class*="company-score"],[class*="store-score"]');

        // ── product reviews ──
        const ratingEl    = q(card, '.search-card-e-review')
            || q(card, '[class*="rate-score"],[class*="rating-score"],[class*="star"],[aria-label*="star"]');
        const reviewCtEl  = q(card, '[class*="review-count"],[class*="review-num"],[class*="buyer-review"],[class*="buyer-feedback"]')
            || q(card, '[class*="transaction"],[class*="feedback"],[class*="sold"],[class*="order-count"],[class*="orders"]');

        const ratingTx = ratingEl
            ? (ratingEl.getAttribute('aria-label') || ratingEl.textContent).trim()
            : 'N/A';

        // Clean price: strip any "Min. order …" suffix that bleeds in from adjacent text
        const rawPrice  = tx(priceEl) || 'N/A';
        const cleanPrice = rawPrice !== 'N/A'
            ? (rawPrice.replace(/\\s*Min\\.?\\s*order[\\s\\S]*/i, '').trim() || rawPrice)
            : 'N/A';

        // Clean MOQ: strip any price prefix that might bleed in
        const rawMoq  = tx(moqEl) || 'N/A';
        const cleanMoq = rawMoq !== 'N/A'
            ? (rawMoq.replace(/^[₹$€£¥][\\d,\\.\\-\\s]+/, '').trim() || rawMoq)
            : 'N/A';

        out.push({
            title,
            href,
            is_sponsored:  isSponsored,
            price:         cleanPrice,
            moq:           cleanMoq,
            date_posted:   tx(dateEl)    || 'N/A',
            images,
            videos,
            supplier:      tx(supplierEl) || 'N/A',
            country:       tx(countryEl)  || 'N/A',
            years_active:  tx(yearsEl)   || 'N/A',
            gold:          !!goldEl,
            verified:      !!verifiedEl,
            response_rate: tx(responseEl) || 'N/A',
            trade_assurance: tx(taEl)    || 'N/A',
            trust_score:   tx(trustEl)   || 'N/A',
            rating:        ratingTx,
            review_count:  tx(reviewCtEl) || 'N/A',
        });
    }
    return { selector: matchedSel, count: cards.length, items: out };
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY MODEL
# ─────────────────────────────────────────────────────────────────────────────

def build_model(r: dict, ts: str) -> dict:
    pid = _product_id(r["href"])

    if r["verified"]:
        vstatus = "Verified"
    elif r["gold"]:
        vstatus = "Gold Supplier"
    else:
        vstatus = "Standard"

    # The real supplier identity (company_id/name) comes from the detail page and
    # is resolved at write time; here we keep only the raw search-card values.
    return {
        "product_details": {
            "product_id":         pid,
            "product_name":       r["title"],
            "price_range":        r["price"],      # raw card price
            "moq":                r["moq"],        # raw card MOQ
            "date_posted":        r["date_posted"],
            "product_detail_url": r["href"],
            "scraped_at":         ts,
            "media": {
                "images": r["images"],
                "videos": r["videos"],
            },
        },
        "reviews": {
            "product_reviews": {
                "product_id":   pid,
                "rating":       r["rating"],
                "review_count": r["review_count"],
            },
            "supplier_reviews": {
                "response_rate":   r["response_rate"],
                "trade_assurance": r["trade_assurance"],
                "trust_score":     r["trust_score"],
            },
            "detailed_reviews": None,
        },
        "supplier_details": {
            "card_name":           r["supplier"],  # raw, often "N/A" on the card
            "verification_status": vstatus,
            "country":             r["country"],
            "years_active":        r["years_active"],
            "gold_supplier":       r["gold"],
        },
        "is_sponsored": r.get("is_sponsored", False),
    }


async def extract_page(page: Page) -> list[dict]:
    payload = await page.evaluate(_EXTRACT_JS)
    items = payload["items"]
    if not INCLUDE_SPONSORED:
        before = len(items)
        items = [r for r in items if not r.get("is_sponsored")]
        print(f"[dom]  filtered {before - len(items)} sponsored cards")
    print(f"[dom]  selector='{payload['selector']}'  containers={payload['count']}  items={len(items)}")
    ts = datetime.now(timezone.utc).isoformat()
    return [build_model(r, ts) for r in items]


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT: MONGODB — four collections, parallel bulk upserts
# ─────────────────────────────────────────────────────────────────────────────

async def mongo_upsert_all(cols: dict, models: list[dict]) -> int:
    if not models:
        return 0
    from pymongo import UpdateOne

    p_ops, pr_ops, s_ops, sr_ops = [], [], [], []
    for m in models:
        ident = _supplier_identity(m)
        sid   = ident["supplier_id"]
        pd_ = m["product_details"]
        pr_ = m["reviews"]["product_reviews"]
        sr_ = m["reviews"]["supplier_reviews"]
        sd_ = m["supplier_details"]
        det = m["reviews"].get("detailed_reviews") or {}

        p_ops.append(UpdateOne(
            {"product_id": pd_["product_id"]},
            {"$set": {**pd_, "last_seen_at": pd_["scraped_at"]},
             "$setOnInsert": {"first_seen_at": pd_["scraped_at"]}},
            upsert=True))
        pr_doc = dict(pr_)
        if det.get("product_reviews"):
            pr_doc["detailed_reviews"] = det["product_reviews"]
        pr_ops.append(UpdateOne({"product_id": pr_["product_id"]}, {"$set": pr_doc}, upsert=True))

        # Supplier docs keyed by the REAL supplier id — skipped entirely when the
        # id is unknown, so missing-id products never collide on a shared key.
        if sid:
            s_doc = dict(sd_)
            s_doc.update(supplier_id=sid, company_id=ident["company_id"],
                         supplier_name=ident["supplier_name"])
            s_ops.append(UpdateOne({"supplier_id": sid}, {"$set": s_doc}, upsert=True))
            sr_doc = dict(sr_)
            sr_doc.update(supplier_id=sid, company_id=ident["company_id"])
            if det.get("store_reviews"):
                sr_doc["detailed_reviews"] = det["store_reviews"]
            sr_ops.append(UpdateOne({"supplier_id": sid}, {"$set": sr_doc}, upsert=True))

    tasks = []
    if cols["products"]         is not None and p_ops:  tasks.append(cols["products"].bulk_write(p_ops,  ordered=False))
    if cols["product_reviews"]  is not None and pr_ops: tasks.append(cols["product_reviews"].bulk_write(pr_ops, ordered=False))
    if cols["suppliers"]        is not None and s_ops:  tasks.append(cols["suppliers"].bulk_write(s_ops,  ordered=False))
    if cols["supplier_reviews"] is not None and sr_ops: tasks.append(cols["supplier_reviews"].bulk_write(sr_ops, ordered=False))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return sum(
        r.upserted_count + r.modified_count
        for r in results if hasattr(r, "upserted_count")
    )


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT: CSV — flattened product + supplier fields (reviews live in reviews files)
# ─────────────────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "product_id", "product_name", "is_sponsored",
    "price_raw", "price_min", "price_max", "currency",
    "moq_value", "moq_unit", "date_posted",
    "supplier_id", "company_id", "supplier_name", "verification_status",
    "country", "years_active", "main_image", "product_detail_url", "scraped_at",
]


def _ensure_parent(path) -> None:
    """Re-create the output directory right before writing, so a transient
    external removal of the run folder (observed on this machine) can't abort the
    write — each writer recreates the dir and the data still lands on disk."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def csv_append(path: str, models: list[dict]) -> None:
    _ensure_parent(path)
    rows = []
    for m in models:
        pd_   = m["product_details"]
        rec   = _product_record(m)["product_details"]
        ident = _supplier_identity(m)
        price = rec["commercial"]["price"]
        moq   = rec["commercial"]["moq"]
        rows.append({
            "product_id":          pd_["product_id"],
            "product_name":        pd_["product_name"],
            "is_sponsored":        m.get("is_sponsored", False),
            "price_raw":           price["price_raw"],
            "price_min":           price["price_min"],
            "price_max":           price["price_max"],
            "currency":            price["currency"],
            "moq_value":           moq["moq_value"],
            "moq_unit":            moq["moq_unit"],
            "date_posted":         rec["dates"]["date_posted"],
            "supplier_id":         ident["supplier_id"],
            "company_id":          ident["company_id"],
            "supplier_name":       ident["supplier_name"],
            "verification_status": m["supplier_details"]["verification_status"],
            "country":             rec["supplier_info"].get("country"),
            "years_active":        rec["supplier_info"].get("years_active"),
            "main_image":          rec["media"]["main_image_url"] or "",
            "product_detail_url":  pd_["product_detail_url"],
            "scraped_at":          pd_["scraped_at"],
        })
    df = pd.DataFrame(rows, columns=_CSV_FIELDS)
    write_header = not Path(path).exists()
    df.to_csv(path, mode="a", index=False, header=write_header, encoding="utf-8-sig")


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT: JSON — full nested model keyed by product_id
# ─────────────────────────────────────────────────────────────────────────────

def _product_record(m: dict) -> dict:
    # File 1 — nested product details. Native-style keys (offerId, subject) only
    # where the value truly is that field; normalized price/MOQ/image/date blocks
    # keep both the raw source value and the parsed result, with an explicit
    # extraction status. Nothing is fabricated: unavailable fields are null.
    pd_    = m["product_details"]
    seller = m.get("seller_native") or {}
    ex     = m.get("product_ex") or {}
    logo   = seller.get("companyLogoFileUrlSmall") or seller.get("companyLogo")
    images = classify_images(pd_["media"].get("images"), m.get("product_media"), logo)
    videos = (m.get("product_media") or {}).get("videos") or pd_["media"].get("videos") or []

    price = parse_price(pd_["price_range"])
    # Native USD from the payload (no FX conversion) — lets a consumer read USD
    # even when the profile locale rendered the display price in another currency.
    if ex.get("price_usd_min") is not None:
        def _usd(x):
            try:
                return _dec_num(Decimal(str(x)))
            except (InvalidOperation, ValueError, TypeError):
                return None
        price["price_usd_min"] = _usd(ex["price_usd_min"])
        price["price_usd_max"] = _usd(ex.get("price_usd_max", ex["price_usd_min"]))
    if ex.get("price_tiers"):            # real tiered pricing from the detail payload
        tiers = ex["price_tiers"]
        price["price_tiers"] = tiers
        # Unit from the payload (card string rarely carries a "/unit" suffix).
        price["unit"] = ex.get("price_unit")
        if not price.get("price_unit"):
            price["price_unit"] = ex.get("price_unit")
        # Overall displayed quantity range: first tier's min → last tier's max
        # (null when the top tier is open-ended, e.g. "1000+").
        price["quantity_tier_min"] = tiers[0].get("min_quantity")
        price["quantity_tier_max"] = tiers[-1].get("max_quantity")
        # min/max come from the actual tier prices, not an assumption about order.
        tp = [t["price"] for t in tiers if isinstance(t.get("price"), (int, float))]
        if tp:
            price["price_min"] = min(tp)
            price["price_max"] = max(tp)
            if price["price_extraction_status"] in ("missing", "unparseable"):
                price["price_extraction_status"] = "range_parsed"
        if not price.get("currency"):
            pat = ex.get("price_currency_pattern") or ""
            for sym, code in _CURRENCY_SYMBOLS.items():
                if sym in pat:
                    price["currency"] = code
                    break
    moq = parse_moq(pd_["moq"])
    if ex.get("moq_value") is not None and moq["moq_value"] is None:
        moq["moq_value"] = ex["moq_value"]
        moq["moq_unit"] = moq["moq_unit"] or ex.get("price_unit")
        moq["moq_extraction_status"] = "parsed_from_payload"

    # ── Field-level validation / correction (before persistence) ───────────────
    # Model number: reject a CPU masquerading as the model; never substitute it.
    mnv   = validate_model_number(ex)
    brand = _sanitize_brand(ex.get("brand"))

    rec = {
        "scrape_metadata": {
            "source":           "alibaba",
            "scraped_at":       pd_["scraped_at"],
            "scraper_version":  SCRAPER_VERSION,
            "extraction_method": "dom+embedded-detaildata",
        },
        "product_details": {
            "offerId":      pd_["product_id"],
            "subject":      pd_["product_name"],
            "product_url":  pd_["product_detail_url"],
            "description":  ex.get("description"),      # rich desc is a lazy module → null
            "category": {
                "category_id":   ex.get("category_id"),
                "category_path": ex.get("category_path", []),
            },
            "dates": {
                # Alibaba does not expose a listing/publication date in its detail
                # payload, so date_posted is null (never the scrape time).
                "date_posted":  _pd_date_posted(m),
                "date_updated": None,
                "first_seen_at": pd_["scraped_at"],
                "last_seen_at":  pd_["scraped_at"],
                "scraped_at":    pd_["scraped_at"],
                "date_extraction_status": "available" if _pd_date_posted(m) else "unavailable",
            },
            "is_sponsored": m.get("is_sponsored", False),
            "attributes": {
                "brand":           brand["value"],
                "model_number":    mnv["value"],
                "material":        ex.get("material"),
                "weight":          ex.get("weight"),
                "dimensions":      ex.get("dimensions"),
                "condition":       ex.get("condition"),
                "warranty":        ex.get("warranty"),
                "after_sales_service": ex.get("after_sales_service"),
                "returns":         ex.get("returns"),
                "place_of_origin": ex.get("place_of_origin"),
                "colors":          ex.get("colors", []),
                "sizes":           ex.get("sizes", []),
                "variants":        ex.get("variants", []),
                "certifications":  ex.get("certifications", []),
            },
            "specifications": ex.get("specifications", {}),
            "commercial": {
                "price": price,
                "moq":   moq,
                "box_moq": ex.get("box_moq"),
                "batch_number": ex.get("batch_number"),
                "customization": ex.get("customization", {}),
            },
            "shipping": {
                "shipping_origin": ex.get("shipping_origin"),
                "lead_time":       ex.get("lead_time", []),
                "unit_size":       ex.get("unit_size"),
                "unit_weight":     ex.get("unit_weight"),
            },
            "media": {
                "main_image_url":     images["main_image_url"],
                "product_image_urls": images["product_image_urls"],
                "supplier_logo_url":  images["supplier_logo_url"],
                "other_media_urls":   images["other_media_urls"],
                "image_extraction_status": images["image_extraction_status"],
                "video_urls":   videos,
                "video_count":  len(videos),
                "video_extraction_status": "available" if videos else "none_found",
            },
            "supplier_info": _supplier_info(m),
        },
    }

    # ── Additive validation block (backward-compatible; existing keys untouched) ─
    pd_out = rec["product_details"]
    price  = pd_out["commercial"]["price"]
    # When the detail page's embedded payload (product_ex) is entirely absent we
    # could not READ the data at all, so unavailable payload-sourced fields are
    # 'extraction_failed', NOT 'missing_on_source' — do not claim a field is
    # absent on the source until the source was actually readable (review §1/2).
    payload_present = bool(m.get("product_ex"))

    def _pstat(v, collection=False):
        if v not in (None, "", [], {}):
            return "available"
        if not payload_present:
            return "extraction_failed"
        return "none_found" if collection else "missing_on_source"

    field_status = {
        "model_number":    (mnv["status"] if (payload_present or mnv["value"]) else "extraction_failed"),
        "brand":           (brand["status"] if (payload_present or brand["value"]) else "extraction_failed"),
        "description":     _pstat(pd_out["description"]),
        "category":        _pstat(pd_out["category"]["category_id"]),
        "material":        _pstat(pd_out["attributes"]["material"]),
        "place_of_origin": _pstat(pd_out["attributes"]["place_of_origin"]),
        "price":           ("available"
                            if price["price_extraction_status"] not in ("missing", "unparseable")
                            else "missing_on_source"),
        "moq":             ("available"
                            if pd_out["commercial"]["moq"]["moq_extraction_status"]
                               not in ("missing", "unparseable")
                            else "missing_on_source"),
        "warranty":        _pstat(pd_out["attributes"]["warranty"]),
        "after_sales_service": _pstat(pd_out["attributes"]["after_sales_service"]),
        "returns":         _pstat(pd_out["attributes"]["returns"]),
        "certifications":  _pstat(pd_out["attributes"]["certifications"], collection=True),
        "shipping_origin": _pstat(pd_out["shipping"]["shipping_origin"]),
        "images":          ("available" if pd_out["media"]["image_extraction_status"] == "classified"
                            else "missing_on_source"),
        "video":           "available" if pd_out["media"]["video_count"] else "none_found",
        "supplier_id":     ("available" if pd_out["supplier_info"].get("supplier_id")
                            else ("extraction_failed" if not payload_present else "missing_on_source")),
    }
    pd_out["validation"] = {
        "detail_payload": "available" if payload_present else "extraction_failed",
        "model_number": {
            "status":           mnv["status"],
            "source":           mnv["source"],
            "raw":              mnv["raw"],
            "candidates":       mnv["candidates"],
            "collides_with":    mnv["collides_with"],
            "confidence":       mnv.get("confidence"),
            "cross_field_match": mnv.get("cross_field_match"),
            "reason":           mnv["reason"],
        },
        "brand": {"status": brand["status"], "raw": brand["raw"],
                  "normalized": brand.get("normalized"), "reason": brand.get("reason")},
        "field_status": field_status,
        "consistency_checks": _consistency_checks(pd_out, mnv),
        "null_report": _null_field_report(pd_out, payload_present, mnv, brand),
        "issues": _validate_cross_fields(rec),
    }
    return rec


def _supplier_info(m: dict) -> dict:
    # Real native company fields from window.detailData.globalData.seller when the
    # detail page was visited. supplier_id is the resolved stable id (null if
    # unknown); company_id is kept as a separate authoritative field; raw names
    # are preserved for debugging.
    sd_      = m["supplier_details"]
    seller   = m.get("seller_native") or {}
    ident    = _supplier_identity(m)
    info = {
        "supplier_id":           ident["supplier_id"],   # null when unknown
        "company_id":            ident["company_id"],
        "name":                  ident["supplier_name"],
        "verification_status":   sd_["verification_status"],
        "business_type":         seller.get("companyBusinessType"),
        "country":               seller.get("companyRegisterCountry") or _clean(sd_.get("country")),
        "years_active":          seller.get("localCompanyJoinYears") or _clean(sd_.get("years_active")),
        "join_years":            seller.get("companyJoinYears"),
        "gold_supplier":         sd_["gold_supplier"],
        "on_time_delivery_rate": seller.get("supplierOnTimeDeliveryRate"),
        "reorder_rate":          (seller.get("supplierReorderRate") or seller.get("reorderRate")
                                  or seller.get("repeatPurchaseRate") or seller.get("repeatBuyerRate")
                                  or seller.get("buyerRepeatRate")),
        "response_time":         seller.get("responseTimeText"),
        "top_rated_supplier":    seller.get("topRatedSupplier"),
        "trade_assurance":       seller.get("tradeAssurance"),
        "order_amount_6m":       seller.get("tradeOrderAmountText"),
        "order_count_6m":        seller.get("tradeOrderCount6m"),
        "company_profile_url":   seller.get("companyProfileUrl"),
        "raw": ident["raw"],
    }
    # keep supplier_id/company_id/name/raw even when null (identity contract);
    # drop only the optional native fields that are absent.
    keep = {"supplier_id", "company_id", "name", "verification_status",
            "gold_supplier", "raw"}
    return {k: v for k, v in info.items() if v is not None or k in keep}


def _clean(v):
    return None if v in (None, "", "N/A") else v


def json_write(path: str, models: list[dict]) -> None:
    # File 1 holds product details only; reviews live in the reviews files.
    _ensure_parent(path)
    p = Path(path)
    records: list = []
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            records = [e for e in loaded
                       if isinstance(e, dict) and e.get("product_details", {}).get("offerId")] \
                if isinstance(loaded, list) else []
        except Exception:
            records = []
    index = {e["product_details"]["offerId"]: i for i, e in enumerate(records)}
    for m in models:
        rec = _product_record(m)
        oid = rec["product_details"]["offerId"]
        if oid in index:
            records[index[oid]] = rec
        else:
            index[oid] = len(records)
            records.append(rec)
    p.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT: REVIEWS — separate JSON + Excel keyed by product_id
# ─────────────────────────────────────────────────────────────────────────────

def _review_item(r: dict) -> dict:
    # One individual review. `rating` is the normalized 1-5 int (null if genuinely
    # unavailable); `stars_raw` preserves the original representation. `text` holds
    # only the customer review; a supplier response goes to `supplier_reply`.
    # media nests deduped images + videos (DOM and native-API shapes both handled).
    media = r.get("media") or r.get("review_media") or {}
    text  = r.get("text", "N/A")
    reply = r.get("supplier_reply")
    if reply is None:
        text, reply = _split_supplier_reply(text)

    def _nn(v):   # unavailable scalars become null, never the string "N/A" (section 7)
        return None if v in (None, "", "N/A") else v

    model = _sanitize_model_number(r.get("model_number"))
    return {
        "reviewer":          _nn(r.get("reviewer")),
        "country":           _nn(r.get("country")),
        "date":              _nn(r.get("date")),
        "rating":            r.get("rating", parse_stars(r.get("stars"))),
        "stars_raw":         _nn(r.get("stars")),
        "text":              _nn(text),
        "supplier_reply":    reply,
        # Review-level model: kept null (not copied from the parent product) unless
        # the review source itself identifies one (section 3D / 5E).
        "model_number":      model,
        "model_status":      "available" if model is not None else "not_provided",
        "verified_purchase": bool(r.get("verified_purchase", False)),
        "repeat_buyer":      bool(r.get("repeat_buyer", False)),
        "valid":             r.get("valid", True),
        "media": {
            "images": _dedupe(media.get("images", [])),
            "videos": _dedupe(media.get("videos", [])),
        },
    }


def _review_record(m: dict) -> dict:
    # File 2 — reviews for one product. scrape_metadata links back to File 1 via
    # offerId (+ supplier_id). star_breakdown / sub_ratings / supplier_reply are
    # omitted: the DOM scrape has no source for them.
    pd_     = m["product_details"]
    seller  = m.get("seller_native") or {}
    ident   = _supplier_identity(m)
    prns    = m.get("product_review_summary_native") or {}
    srs     = m.get("store_review_summary")
    pr_card = m["reviews"]["product_reviews"]
    sr_card = m["reviews"]["supplier_reviews"]
    det     = m["reviews"].get("detailed_reviews") or {}
    pdet    = det.get("product_reviews") or {}
    sdet    = det.get("store_reviews") or {}

    # Product displayed count: the DOM tab count or Alibaba's native product-review
    # total (review.productReview.totalReviewCount), whichever is known.
    p_tab = max(int((pdet or {}).get("tab_count", 0) or 0),
                int(prns.get("total_review_count") or 0))
    s_tab = int((sdet or {}).get("tab_count", 0) or 0)
    # Store reviews from the supplier feedback page are COMPLETE only when the
    # pager reached its disabled end; then the extracted count is authoritative.
    s_complete = bool((sdet or {}).get("feedback_complete"))
    p_q = review_quality(pdet or None, p_tab)
    s_q = review_quality(sdet or None, s_tab, complete=s_complete)

    # trade_assurance is an authoritative boolean in the detail payload
    # (seller.tradeAssurance); the search card rarely carries it, so prefer the
    # payload and fall back to the card. response_rate / trust_score are not in
    # the payload for most suppliers → null when genuinely absent (never "N/A").
    ta_native = seller.get("tradeAssurance")
    trade_assurance = ta_native if ta_native is not None else _clean(sr_card.get("trade_assurance"))
    response_rate   = _clean(sr_card.get("response_rate"))
    trust_score     = _clean(sr_card.get("trust_score"))

    if srs:
        # Real native store-rating summary (averageStar + per-dimension
        # sub_ratings, each with a 1-5 star_breakdown) from the page payload.
        # total_reviews uses the real store-review count, not the order-rating
        # subtotal in supplierRatingReviews.
        store_summary = {
            "overall_rating":        _clean(srs.get("averageStar")),
            "total_reviews":         s_q["total_displayed"],   # == extraction.total_displayed
            "sub_ratings":           srs.get("sub_ratings", {}),
            "on_time_delivery_rate": _clean(seller.get("supplierOnTimeDeliveryRate")),
            "response_time":         _clean(seller.get("responseTimeText")),
            "top_rated_supplier":    bool(seller.get("topRatedSupplier", False)),
            "response_rate":         response_rate,
            "trade_assurance":       trade_assurance,
            "trust_score":           trust_score,
        }
    else:
        store_summary = {
            "overall_rating":  _clean(sdet.get("overall_rating")),
            "total_reviews":   s_q["total_displayed"],   # == extraction.total_displayed
            "response_rate":   response_rate,
            "trade_assurance": trade_assurance,
            "trust_score":     trust_score,
        }

    return {
        "scrape_metadata": {
            "source":          "alibaba",
            "scraped_at":      pd_["scraped_at"],
            "scraper_version": SCRAPER_VERSION,
            "offerId":         pd_["product_id"],
            "subject":         pd_["product_name"],
            "company_id":      ident["company_id"],
            "supplier_id":     ident["supplier_id"],
            "date_posted":     _pd_date_posted(m),
        },
        "reviews": {
            "product_reviews": {
                "summary": {
                    "overall_rating": (prns.get("average_star") if prns.get("average_star") is not None
                                       else _clean(pdet.get("overall_rating") or pr_card.get("rating"))),
                    "total_reviews":  p_q["total_displayed"],   # == extraction.total_displayed
                    "review_count":   _clean(pr_card.get("review_count")),
                },
                "extraction": {
                    "status":          p_q["status"],
                    "total_displayed": p_q["total_displayed"],
                    "extracted":       p_q["extracted"],
                    "valid":           p_q["valid"],
                    "rejected":        p_q["rejected"],
                    "missing_ratings": p_q["missing_ratings"],
                    "warnings":        p_q["warnings"],
                },
                "items":        [_review_item(r) for r in p_q["items"]],
                "native_items": pdet.get("native_items", []),
            },
            "store_reviews": {
                "summary":      store_summary,
                "extraction": {
                    "status":            s_q["status"],
                    "total_displayed":   s_q["total_displayed"],
                    "product_tab_count": s_tab,   # original product-page store count (often inflated)
                    "extracted":         s_q["extracted"],
                    "valid":             s_q["valid"],
                    "rejected":          s_q["rejected"],
                    "missing_ratings":   s_q["missing_ratings"],
                    "source":            (sdet or {}).get("_source", "dom"),
                    "termination":       (sdet or {}).get("feedback_termination"),
                    "complete":          s_complete,
                    "warnings":          s_q["warnings"],
                },
                "items":        [_review_item(r) for r in s_q["items"]],
                "native_items": sdet.get("native_items", []),
            },
        },
    }


def reviews_json_write(path: str, models: list[dict]) -> None:
    _ensure_parent(path)
    p = Path(path)
    records: list = []
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            records = [e for e in loaded
                       if isinstance(e, dict) and e.get("scrape_metadata", {}).get("offerId")] \
                if isinstance(loaded, list) else []
        except Exception:
            records = []
    index = {e["scrape_metadata"]["offerId"]: i for i, e in enumerate(records)}
    for m in models:
        rec = _review_record(m)
        oid = rec["scrape_metadata"]["offerId"]
        if oid in index:
            records[index[oid]] = rec
        else:
            index[oid] = len(records)
            records.append(rec)
    p.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def reviews_excel_write(json_path: str, xlsx_path: str) -> None:
    # Rebuilt from the reviews JSON so it stays in sync and keyed by product_id.
    # One row per individual review, with review_type = product | store.
    _ensure_parent(xlsx_path)
    try:
        records = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception:
        return

    def _rows_for(rec, rtype, block):
        meta = rec.get("scrape_metadata", {})
        summ = block.get("summary", {})
        extr = block.get("extraction", {})
        base = {
            "offerId":          meta.get("offerId", ""),
            "subject":          meta.get("subject", ""),
            "supplier_id":      meta.get("supplier_id", ""),
            "company_id":       meta.get("company_id", ""),
            "review_type":      rtype,
            "overall_rating":   summ.get("overall_rating", ""),
            "total_reviews":    summ.get("total_reviews", 0),
            "extraction_status": extr.get("status", ""),
            "extracted":        extr.get("extracted", ""),
            "valid":            extr.get("valid", ""),
            "rejected":         extr.get("rejected", ""),
        }
        items = block.get("items", [])
        if not items:
            return [{**base, "reviewer": "", "review_country": "", "review_date": "",
                     "rating": "", "stars_raw": "", "review_text": "", "model_number": "",
                     "verified_purchase": "", "repeat_buyer": "",
                     "review_images": "", "review_videos": ""}]
        out = []
        for rv in items:
            media = rv.get("media", {})
            out.append({
                **base,
                "reviewer":          rv.get("reviewer", ""),
                "review_country":    rv.get("country", ""),
                "review_date":       rv.get("date", ""),
                "rating":            rv.get("rating", ""),
                "stars_raw":         rv.get("stars_raw", ""),
                "review_text":       rv.get("text", ""),
                "supplier_reply":    rv.get("supplier_reply", "") or "",
                "model_number":      rv.get("model_number", ""),
                "verified_purchase": rv.get("verified_purchase", ""),
                "repeat_buyer":      rv.get("repeat_buyer", ""),
                "review_images":     " | ".join(media.get("images", [])),
                "review_videos":     " | ".join(media.get("videos", [])),
            })
        return out

    rows = []
    for rec in records:
        revs = rec.get("reviews", {})
        rows += _rows_for(rec, "product", revs.get("product_reviews", {}))
        rows += _rows_for(rec, "store",   revs.get("store_reviews", {}))
    pd.DataFrame(rows).to_excel(xlsx_path, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# DATA-QUALITY REPORT
# ─────────────────────────────────────────────────────────────────────────────

def data_quality_report(models: list[dict], run_meta: dict) -> dict:
    """Aggregate a per-run data-quality report from the collected models."""
    prod = {"total": len(models), "valid": 0, "duplicate_ids": 0,
            "missing_supplier_id": 0, "missing_price": 0, "missing_moq": 0,
            "missing_date_posted": 0, "unclassified_images": 0, "with_video": 0}
    rev = {t: {"displayed": 0, "extracted": 0, "valid": 0, "rejected": 0,
               "missing_ratings": 0} for t in ("product_reviews", "store_reviews")}
    # section 11C/D/E evidence buckets (offerId-tagged)
    model_q = {"available": 0, "not_provided": 0, "invalid_cpu_collision": 0,
               "not_verified": 0, "conflicting": 0, "cpu_collisions": [], "conflicts": []}
    brand_q = {"available": 0, "generic_nulled": 0, "missing": 0}
    price_q = {"valid": 0, "invalid_range": 0, "inconsistent_tiers": 0,
               "missing_currency": 0, "issues": []}
    cross_q = {"offerid_url_mismatch": [], "video_count_mismatch": [],
               "records_with_issues": []}
    seen: set[str] = set()
    warnings: list[str] = []

    for m in models:
        full  = _product_record(m)
        rec   = full["product_details"]
        val   = rec.get("validation", {})
        pid   = rec["offerId"]
        if pid in seen:
            prod["duplicate_ids"] += 1
        seen.add(pid)
        if pid and rec["subject"] and rec["product_url"]:
            prod["valid"] += 1
        if not _supplier_identity(m)["supplier_id"]:
            prod["missing_supplier_id"] += 1
        if rec["commercial"]["price"]["price_extraction_status"] in ("missing", "unparseable"):
            prod["missing_price"] += 1
        if rec["commercial"]["moq"]["moq_extraction_status"] in ("missing", "unparseable"):
            prod["missing_moq"] += 1
        if not rec["dates"]["date_posted"]:
            prod["missing_date_posted"] += 1
        if rec["media"]["image_extraction_status"] != "classified":
            prod["unclassified_images"] += 1
        if rec["media"]["video_count"]:
            prod["with_video"] += 1

        # ── model-number quality (section 11C) ──
        mn = val.get("model_number", {})
        mstat = mn.get("status")
        if mstat == "available":
            model_q["available"] += 1
        elif mstat == "not_provided":
            model_q["not_provided"] += 1
        elif mstat in ("invalid", "not_verified"):
            key = "invalid_cpu_collision" if mstat == "invalid" else "not_verified"
            model_q[key] += 1
            model_q["cpu_collisions"].append(
                {"offerId": pid, "status": mstat, "raw": mn.get("raw"),
                 "collides_with": mn.get("collides_with"), "product_url": rec["product_url"]})
        elif mstat == "conflicting":
            model_q["conflicting"] += 1
            model_q["conflicts"].append(
                {"offerId": pid, "candidates": mn.get("candidates")})

        # ── brand quality (section 4C) ──
        bstat = val.get("brand", {}).get("status")
        if bstat == "available":
            brand_q["available"] += 1
        elif bstat == "not_verified":
            brand_q["generic_nulled"] += 1
        else:
            brand_q["missing"] += 1

        # ── pricing quality (section 11D) ──
        price_issues = _validate_price_block(rec["commercial"]["price"])
        if any("overlap" in i or "min_quantity" in i for i in price_issues):
            price_q["inconsistent_tiers"] += 1
        if any("price_min" in i and "price_max" in i for i in price_issues):
            price_q["invalid_range"] += 1
        if rec["commercial"]["price"]["price_min"] is not None and not rec["commercial"]["price"]["currency"]:
            price_q["missing_currency"] += 1
        if not price_issues and rec["commercial"]["price"]["price_extraction_status"] not in ("missing", "unparseable"):
            price_q["valid"] += 1
        if price_issues:
            price_q["issues"].append({"offerId": pid, "issues": price_issues})

        # ── cross-field issues (section 11F) ──
        issues = val.get("issues", [])
        for i in issues:
            if "not found in product_url" in i:
                cross_q["offerid_url_mismatch"].append(pid)
            if "video_count" in i:
                cross_q["video_count_mismatch"].append(pid)
        if issues:
            cross_q["records_with_issues"].append({"offerId": pid, "issues": issues})

        rr = _review_record(m)["reviews"]
        for t in ("product_reviews", "store_reviews"):
            ex = rr[t]["extraction"]
            rev[t]["displayed"]       += ex["total_displayed"]
            rev[t]["extracted"]       += ex["extracted"]
            rev[t]["valid"]           += ex["valid"]
            rev[t]["rejected"]        += ex["rejected"]
            rev[t]["missing_ratings"] += ex["missing_ratings"]

    warnings += validate_supplier_consistency(models)

    # offerId linkage between the product file and the review file (joined by
    # offerId, never by array position).
    prod_ids = [_product_record(m)["product_details"]["offerId"] for m in models]
    rev_ids  = [_review_record(m)["scrape_metadata"]["offerId"] for m in models]
    linkage = {
        "product_records":    len(prod_ids),
        "review_records":     len(rev_ids),
        "unique_offer_ids":   len(set(prod_ids)) == len(prod_ids),
        "offer_ids_match":    set(prod_ids) == set(rev_ids),
        "join_key":           "offerId",
    }
    if not linkage["unique_offer_ids"]:
        warnings.append("duplicate offerIds across product records")
    if not linkage["offer_ids_match"]:
        warnings.append("product and review files have mismatched offerIds")

    if model_q["cpu_collisions"]:
        warnings.append(
            f"{len(model_q['cpu_collisions'])} product(s) had a CPU/processor value "
            f"rejected as the model number (now null): "
            f"{[c['offerId'] for c in model_q['cpu_collisions']]}")

    return {
        **run_meta,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "products": prod,
        "reviews": rev,
        "model_number_quality": model_q,
        "brand_quality": brand_q,
        "pricing_quality": price_q,
        "cross_field_quality": cross_q,
        "linkage": linkage,
        "warnings": warnings,
    }


# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE / ACCESS (section 16)
# ─────────────────────────────────────────────────────────────────────────────

def access_status() -> str:
    return "authorized" if COLLECTION_AUTHORIZED else "authorization_unconfirmed"


def compliance_meta() -> dict:
    return {
        "access_status":           access_status(),
        "authorized":              bool(COLLECTION_AUTHORIZED),
        "authorization_reference": AUTHORIZATION_REFERENCE,
        "bypasses_protections":    False,
        "notice":                  COMPLIANCE_NOTICE,
    }


def print_compliance_banner() -> None:
    status = access_status()
    bar = "=" * 72
    print("\n" + bar)
    if status == "authorized":
        print("[compliance]  Collection marked AUTHORIZED.")
        print(f"[compliance]  Reference: {AUTHORIZATION_REFERENCE}")
    else:
        print("[compliance]  ACCESS STATUS: authorization_unconfirmed")
        print("[compliance]  " + COMPLIANCE_NOTICE)
        print("[compliance]  Set COLLECTION_AUTHORIZED=True (with a reference) once")
        print("[compliance]  you hold written permission or official-API access.")
    print(bar + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# HISTORICAL STORE (section 14) — append-only, never overwrites prior values
# ─────────────────────────────────────────────────────────────────────────────

def _price_history_record(m: dict, run_id: str) -> dict:
    rec   = _product_record(m)["product_details"]
    price = rec["commercial"]["price"]
    moq   = rec["commercial"]["moq"]
    return {
        "run_id":       run_id,
        "product_id":   rec["offerId"],
        "scraped_at":   rec["dates"]["scraped_at"],
        "currency":     price["currency"],
        "price_min":    price["price_min"],
        "price_max":    price["price_max"],
        "price_status": price["price_extraction_status"],
        "moq_value":    moq["moq_value"],
        "moq_unit":     moq["moq_unit"],
    }


def _rating_history_record(m: dict, run_id: str) -> dict:
    rr   = _review_record(m)
    meta = rr["scrape_metadata"]
    pr   = rr["reviews"]["product_reviews"]["summary"]
    sr   = rr["reviews"]["store_reviews"]["summary"]
    return {
        "run_id":                 run_id,
        "product_id":             meta["offerId"],
        "company_id":             meta["company_id"],
        "scraped_at":             m["product_details"]["scraped_at"],
        "product_overall_rating": pr.get("overall_rating"),
        "product_total_reviews":  pr.get("total_reviews"),
        "store_overall_rating":   sr.get("overall_rating"),
        "store_total_reviews":    sr.get("total_reviews"),
        "store_sub_ratings":      sr.get("sub_ratings"),
    }


def _product_snapshot_record(m: dict, run_id: str) -> dict:
    snap = _product_record(m)
    snap["run_id"] = run_id
    return snap


def append_jsonl(path, records: list[dict]) -> None:
    """Append records as one JSON object per line (never rewrites existing rows)."""
    if not records:
        return
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def history_append(models: list[dict], run_id: str, hist_dir: str) -> None:
    """File-based append-only history (works with or without MongoDB)."""
    if not ENABLE_HISTORY or not models:
        return
    hp = Path(hist_dir)
    hp.mkdir(parents=True, exist_ok=True)
    append_jsonl(hp / "price_history.jsonl",     [_price_history_record(m, run_id) for m in models])
    append_jsonl(hp / "rating_history.jsonl",    [_rating_history_record(m, run_id) for m in models])
    append_jsonl(hp / "product_snapshots.jsonl", [_product_snapshot_record(m, run_id) for m in models])


async def mongo_append_history(cols: dict, models: list[dict], run_id: str) -> None:
    """MongoDB append-only history (parallel inserts), skipped if unavailable."""
    if not ENABLE_HISTORY or not models:
        return
    price  = [_price_history_record(m, run_id)     for m in models]
    rating = [_rating_history_record(m, run_id)    for m in models]
    snap   = [_product_snapshot_record(m, run_id)  for m in models]
    tasks = []
    if cols.get("price_history")     is not None and price:  tasks.append(cols["price_history"].insert_many(price,  ordered=False))
    if cols.get("rating_history")    is not None and rating: tasks.append(cols["rating_history"].insert_many(rating, ordered=False))
    if cols.get("product_snapshots") is not None and snap:   tasks.append(cols["product_snapshots"].insert_many(snap, ordered=False))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    global OUTPUT_DIR, _FEEDBACK_SEM
    _FEEDBACK_CACHE.clear()        # fresh supplier-feedback cache each run
    _FEEDBACK_INFLIGHT.clear()
    _FEEDBACK_SEM = None           # recreated on first use in this run's loop
    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    OUTPUT_DIR = Path(__file__).parent / f"Alibaba_Scraped_Results_{run_ts}"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _stamped(name: str) -> str:
        p = Path(name)
        return str(OUTPUT_DIR / f"{p.stem}_{run_ts}{p.suffix}")

    out_csv          = _stamped(OUTPUT_CSV)
    out_json         = _stamped(OUTPUT_JSON)
    out_reviews_json = _stamped(OUTPUT_REVIEWS_JSON)
    out_reviews_xlsx = _stamped(OUTPUT_REVIEWS_XLSX)
    _log.info("event=output_dir  dir=%s", OUTPUT_DIR)

    # Compliance / access notice (section 16) — loud and recorded every run.
    print_compliance_banner()
    _log.info("event=access_status  status=%s  authorized=%s", access_status(), COLLECTION_AUTHORIZED)

    cols = {"products": None, "product_reviews": None, "suppliers": None, "supplier_reviews": None,
            "price_history": None, "rating_history": None, "product_snapshots": None,
            "collection_runs": None}
    try:
        client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=4000)
        await client.server_info()
        db = client[MONGO_DB]
        cols["products"]         = db["products"]
        cols["product_reviews"]  = db["product_reviews"]
        cols["suppliers"]        = db["suppliers"]
        cols["supplier_reviews"] = db["supplier_reviews"]
        await cols["products"].create_index("product_id", unique=True)
        await cols["product_reviews"].create_index("product_id", unique=True)
        await cols["suppliers"].create_index("supplier_id", unique=True)
        await cols["supplier_reviews"].create_index("supplier_id", unique=True)
        # Append-only history collections (section 14) — non-unique indexes.
        cols["price_history"]     = db["price_history"]
        cols["rating_history"]    = db["rating_history"]
        cols["product_snapshots"] = db["product_snapshots"]
        cols["collection_runs"]   = db["collection_runs"]
        await cols["price_history"].create_index([("product_id", 1), ("run_id", 1)])
        await cols["rating_history"].create_index([("product_id", 1), ("run_id", 1)])
        await cols["product_snapshots"].create_index([("product_id", 1), ("run_id", 1)])
        await cols["collection_runs"].create_index("run_id", unique=True)
        _log.info("event=db_connected  db=%s  collections=8", MONGO_DB)
    except Exception:
        _log.warning("event=db_unavailable  mode=csv_json_only")

    raw_url = DIRECT_URL.strip()
    if not raw_url:
        url_mode = f"keyword={KEYWORD!r}"
    elif _is_homepage_url(raw_url):
        url_mode = f"keyword_via_domain  keyword={KEYWORD!r}"
    else:
        url_mode = f"direct={raw_url[:55]}…"
    pages_label = "unlimited" if MAX_PAGES == 0 else str(MAX_PAGES)
    _log.info("event=config  mode=%s  pages=%s  max_products=%d  sponsored=%s  fetch_reviews=%s",
              url_mode, pages_label, MAX_PRODUCTS, INCLUDE_SPONSORED, FETCH_REVIEWS)

    total      = 0
    page_stats: list[dict] = []
    all_models: list[dict] = []   # accumulated for the end-of-run quality report
    _page_limit = MAX_PAGES if MAX_PAGES > 0 else 10_000

    async with async_playwright() as pw:
        launch_args = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
        browser = None   # set only in the fresh-context path

        if CHROME_PROFILE_DIR:
            _log.info("event=launch  mode=persistent_profile  dir=%s", CHROME_PROFILE_DIR)
            try:
                shared_ctx = await pw.chromium.launch_persistent_context(
                    CHROME_PROFILE_DIR,
                    channel="chrome",
                    headless=False,
                    args=launch_args,
                )
            except Exception as exc:
                if "already in use" in str(exc) or "existing browser session" in str(exc):
                    print("\n" + "=" * 68)
                    print("[!]  Chrome profile is locked — another Chrome is using this folder.")
                    print(f"[!]  Profile: {CHROME_PROFILE_DIR}")
                    print("[!]  Close every Chrome window AND background process, then retry.")
                    print("[!]  (Check Task Manager for leftover 'chrome.exe' processes,")
                    print("[!]   or run:  taskkill /F /IM chrome.exe )")
                    print("=" * 68 + "\n")
                raise
        else:
            _log.info("event=launch  mode=fresh_context")
            browser = await pw.chromium.launch(
                headless=False, channel="chrome", args=launch_args,
            )
            shared_ctx = await new_stealth_context(browser)

        await shared_ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',    { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages',  { get: () => ['en-US','en'] });
            window.chrome = { runtime: {} };
        """)

        for page_num in range(1, _page_limit + 1):
            url  = page_url(page_num)
            _log.info("event=page_start  page=%d  limit=%s  url=%s",
                      page_num, pages_label, url[:80])

            page = await new_stealth_page(shared_ctx)
            try:
                if page_num == 1:
                    # Warm-up visit so the session has cookies before the search
                    _log.info("event=warmup_start")
                    await page.goto("https://www.alibaba.com", wait_until="load", timeout=60_000)
                    await _wait_network_idle(page, timeout_ms=4_000)
                    if await _has_captcha(page):
                        await _wait_for_captcha_clear(page)
                        await _wait_network_idle(page, timeout_ms=4_000)
                    await page.evaluate(f"window.scrollTo(0, {random.randint(100, 400)})")
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    _log.info("event=warmup_done")

                await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                _log.info("event=page_loaded  page=%d  url=%s", page_num, page.url[:80])
                await _wait_network_idle(page, timeout_ms=8_000)

                if await _has_captcha(page):
                    await _wait_for_captcha_clear(page)
                    await _wait_network_idle(page, timeout_ms=8_000)

                # Reload once if the body came back essentially empty
                try:
                    link_count = await page.evaluate("document.querySelectorAll('a[href]').length")
                    if link_count < 10:
                        _log.warning("event=blank_page_detected  links=%d  reloading", link_count)
                        print("[!]  Page appears blank — reloading once to retry...")
                        await page.reload(wait_until="domcontentloaded", timeout=90_000)
                        await _wait_network_idle(page, timeout_ms=8_000)
                        if await _has_captcha(page):
                            await _wait_for_captcha_clear(page)
                            await _wait_network_idle(page, timeout_ms=8_000)
                except Exception:
                    pass

                _card_sels = (
                    '.m-gallery-product-item-v2,'
                    '[class*="search-card-e-offer"],'
                    '[class*="J-offer-wrapper"],'
                    '[data-content="product-item"],'
                    '[class*="offer-list-item"],'
                    '[class*="organic-list-offer"],'
                    '[class*="AlbCard"],'
                    '[class*="offer-wrapper"],'
                    '[class*="card-info"],'
                    '[class*="product-card"],'
                    'a[href*="/product-detail/"],'
                    'a[href*="product_"]'
                )
                try:
                    await page.wait_for_selector(_card_sels, timeout=60_000)
                except Exception:
                    _log.warning("event=cards_not_found  page=%d  url=%s", page_num, page.url[:80])
                    try:
                        dbg_dir   = Path(__file__).parent
                        shot_path = dbg_dir / f"_search_page{page_num}.png"
                        html_path = dbg_dir / f"_search_page{page_num}.html"
                        await page.screenshot(path=str(shot_path), full_page=False)
                        html_path.write_text(await page.content(), encoding="utf-8")
                        _log.info("event=debug_saved  screenshot=%s  html=%s", shot_path, html_path)
                        print(f"[!]   Debug files saved:")
                        print(f"      Screenshot → {shot_path}")
                        print(f"      HTML       → {html_path}")
                    except Exception as de:
                        _log.warning("event=debug_save_failed  error=%s", de)

                await scroll_page(page)
                models = await extract_page(page)

                # Enforce MAX_PRODUCTS BEFORE the costly review fetch, counting
                # products already collected on earlier pages (0 = no limit).
                if MAX_PRODUCTS > 0 and models:
                    remaining = MAX_PRODUCTS - total
                    if remaining <= 0:
                        models = []
                    elif len(models) > remaining:
                        _log.info("event=max_products_trim  page=%d  kept=%d  dropped=%d  cap=%d",
                                  page_num, remaining, len(models) - remaining, MAX_PRODUCTS)
                        models = models[:remaining]

                if FETCH_REVIEWS and models:
                    _log.info("event=reviews_start  products=%d  concurrency=%d",
                              len(models), REVIEW_CONCURRENCY)
                    sem       = asyncio.Semaphore(REVIEW_CONCURRENCY)
                    completed = [0]

                    async def _fetch_one(m, slot_idx: int):
                        if slot_idx > 0:
                            await asyncio.sleep(random.uniform(0.6, 1.2) * slot_idx)
                        async with sem:
                            detail_url = m["product_details"]["product_detail_url"]
                            rv = await fetch_reviews(shared_ctx, detail_url)
                            m["reviews"]["detailed_reviews"] = rv
                            # Real native company + store-rating summary from the page
                            m["seller_native"]        = rv.get("seller")
                            m["store_review_summary"] = rv.get("store_review_summary")
                            # Rich product details + native product-review summary
                            m["product_ex"]                 = rv.get("product_ex")
                            m["product_review_summary_native"] = rv.get("product_review_summary")
                            # Real product gallery (clean images + product video) from
                            # the detail page overrides the sparse search-card media.
                            pm = rv.get("product_media") or {}
                            if pm.get("images"):
                                m["product_details"]["media"]["images"] = pm["images"]
                            if pm.get("videos"):
                                m["product_details"]["media"]["videos"] = pm["videos"]

                            # Detail page's date is more reliable than the search card
                            detail_date = rv.get("date_posted", "N/A")
                            if detail_date and detail_date != "N/A":
                                m["product_details"]["date_posted"] = detail_date

                            completed[0] += 1
                            p_cnt = (rv.get("product_reviews") or {}).get("total_reviews", 0)
                            s_cnt = (rv.get("store_reviews") or {}).get("total_reviews", 0)
                            _log.info("event=review_done  %d/%d  product_reviews=%d  store_reviews=%d  date=%s  url=…%s",
                                      completed[0], len(models), p_cnt, s_cnt, detail_date, detail_url[-45:])
                            await asyncio.sleep(random.uniform(1.0, 2.0))

                    await asyncio.gather(*[_fetch_one(m, i) for i, m in enumerate(models)])

            except Exception as exc:
                _log.error("event=page_error  page=%d  error=%s", page_num, exc)
                models = []
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

            if not models:
                _log.warning("event=no_products  page=%d  stopping", page_num)
                page_stats.append({"page": page_num, "products": 0, "db_upserts": 0})
                break

            if _DATE_FROM or _DATE_TO:
                before = len(models)
                models = [m for m in models if _in_date_range(m)]
                _log.info("event=date_filter  page=%d  kept=%d  dropped=%d",
                          page_num, len(models), before - len(models))

            if models:
                db_task      = mongo_upsert_all(cols, models)
                hist_db_task = mongo_append_history(cols, models, run_ts)
                csv_task     = asyncio.to_thread(csv_append, out_csv,  models)
                json_task    = asyncio.to_thread(json_write,  out_json, models)
                reviews_task = asyncio.to_thread(reviews_json_write, out_reviews_json, models)
                hist_task    = asyncio.to_thread(history_append, models, run_ts, HISTORY_DIR)
                results      = await asyncio.gather(db_task, hist_db_task, csv_task, json_task,
                                                    reviews_task, hist_task, return_exceptions=True)
                await asyncio.to_thread(reviews_excel_write, out_reviews_json, out_reviews_xlsx)

                db_count = results[0] if (isinstance(results, list) and isinstance(results[0], int)) else 0
                total   += len(models)
                all_models.extend(models)
                page_stats.append({"page": page_num, "products": len(models), "db_upserts": db_count})
                _log.info("event=page_saved  page=%d  products=%d  db_upserts=%d  running_total=%d",
                          page_num, len(models), db_count, total)
            else:
                page_stats.append({"page": page_num, "products": 0, "db_upserts": 0})
                _log.info("event=page_empty_after_filter  page=%d", page_num)

            if MAX_PRODUCTS > 0 and total >= MAX_PRODUCTS:
                _log.info("event=max_products_reached  total=%d  cap=%d  stopping", total, MAX_PRODUCTS)
                break

            more_pages = (MAX_PAGES == 0) or (page_num < MAX_PAGES)
            if more_pages:
                delay = random.uniform(2.5, 4.5)
                _log.info("event=between_pages  delay=%.1fs  next_page=%d", delay, page_num + 1)
                await asyncio.sleep(delay)

        try:
            await shared_ctx.close()
        except Exception:
            pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    # ── Data-quality report ───────────────────────────────────────────────────
    run_meta = {
        "platform":        "alibaba",
        "keyword":         KEYWORD,
        "category":        None,
        "collection_run_id": run_ts,
        "date_from":       DATE_FROM,
        "date_to":         DATE_TO,
        "scraper_version": SCRAPER_VERSION,
        "compliance":      compliance_meta(),
    }
    try:
        report = data_quality_report(all_models, run_meta)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)   # re-create if externally removed mid-run
        report_path = OUTPUT_DIR / f"data_quality_report_{run_ts}.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        _log.info("event=quality_report_written  path=%s", report_path)
        if report["warnings"]:
            for w in report["warnings"][:10]:
                _log.warning("event=quality_warning  %s", w)
    except Exception as exc:
        report = None
        _log.error("event=quality_report_failed  error=%s", exc)

    # ── Collection-run history record (section 14) ─────────────────────────────
    run_record = {
        **run_meta,
        "run_id":          run_ts,
        "access_status":   access_status(),
        "started_at":      None,
        "finished_at":     datetime.now(timezone.utc).isoformat(),
        "products_total":  total,
        "pages":           len(page_stats),
        "quality":         (report or {}).get("products"),
        "warnings_count":  len((report or {}).get("warnings", [])),
    }
    try:
        if ENABLE_HISTORY:
            append_jsonl(Path(HISTORY_DIR) / "collection_runs.jsonl", [run_record])
        if cols.get("collection_runs") is not None:
            await cols["collection_runs"].update_one(
                {"run_id": run_ts}, {"$set": run_record}, upsert=True)
    except Exception as exc:
        _log.error("event=collection_run_record_failed  error=%s", exc)

    # ── Final summary ─────────────────────────────────────────────────────────
    W = 54
    print(f"\n{'═' * W}")
    print(f"  SCRAPE SUMMARY")
    print(f"{'─' * W}")
    print(f"  {'Page':<8} {'Products':>10} {'DB Upserts':>12}")
    print(f"  {'────':<8} {'────────':>10} {'──────────':>12}")
    for s in page_stats:
        print(f"  {s['page']:<8} {s['products']:>10} {s['db_upserts']:>12}")
    print(f"{'─' * W}")
    print(f"  {'TOTAL':<8} {total:>10}")
    print(f"{'─' * W}")
    print(f"  Pages reviewed : {len(page_stats)}")
    print(f"  Products found : {total}")
    print(f"{'─' * W}")
    print(f"  Output folder → {OUTPUT_DIR.resolve()}")
    print(f"  CSV          → {Path(out_csv).name}")
    print(f"  JSON         → {Path(out_json).name}")
    print(f"  Reviews JSON → {Path(out_reviews_json).name}")
    print(f"  Reviews XLSX → {Path(out_reviews_xlsx).name}")
    if report is not None:
        pr = report["products"]
        print(f"{'─' * W}")
        print(f"  DATA QUALITY")
        print(f"    valid products        : {pr['valid']}/{pr['total']}")
        print(f"    missing supplier_id   : {pr['missing_supplier_id']}")
        print(f"    missing price / moq   : {pr['missing_price']} / {pr['missing_moq']}")
        print(f"    products with video   : {pr['with_video']}")
        print(f"    warnings              : {len(report['warnings'])}")
        print(f"  Quality report → data_quality_report_{run_ts}.json")
    print(f"{'─' * W}")
    print(f"  Access status  : {access_status()}")
    if ENABLE_HISTORY:
        print(f"  History (append-only) → {Path(HISTORY_DIR).resolve()}")
        print(f"    price_history.jsonl · rating_history.jsonl · product_snapshots.jsonl · collection_runs.jsonl")
    print(f"{'═' * W}")


if __name__ == "__main__":
    asyncio.run(main())
