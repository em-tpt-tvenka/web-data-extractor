from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_BAD_NAMES = {"", "n/a", "na", "null", "none", "-", "unknown"}

def product_id_from_href(href: str) -> str:
    m = re.search(r"_(\d{8,})(?:\.html|$|\?)", href)
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    pid = (qs.get("productId") or qs.get("offerId") or [""])[0]
    if pid:
        return pid
    clean = href.split("?")[0].rstrip("/")
    return hashlib.md5(clean.encode()).hexdigest()[:16]

def _norm_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())

def resolve_supplier_identity(company_id, card_name, detail_name) -> dict:
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
        "supplier_id": supplier_id,
        "company_id": cid,
        "supplier_name": real_name if norm not in _BAD_NAMES else None,
        "raw": {"card_name": card_name, "detail_name": detail_name},
    }

_DATE_FORMATS = (
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
)

# Time-of-day / zone suffix some sources append to a date, e.g. Alibaba's
# supplier feedback "12 Nov 2026 01:06" or an API "2026-11-12 01:06:00".
_TIME_SUFFIX_RE = re.compile(
    r"[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:[AaPp][Mm])?"
    r"(?:\s*(?:Z|UTC|GMT|[+-]\d{2}:?\d{2}))?\s*$"
)

def parse_posted_date(s: str | None):
    if not s or s == "N/A":
        return None
    s = re.sub(r"(?i)^\s*(posted|updated|published)[:\s]+", "", s.strip())
    s = _TIME_SUFFIX_RE.sub("", s).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])).date()
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        try:
            return datetime.strptime(f"{m[1][:3]} {m[2]} {m[3]}", "%b %d %Y").date()
        except ValueError:
            return None
    return None

_CURRENCY_SYMBOLS = {
    "US$": "USD", "C$": "CAD", "A$": "AUD", "₹": "INR", "$": "USD",
    "€": "EUR", "£": "GBP", "¥": "CNY", "₫": "VND", "₩": "KRW",
}
_CURRENCY_WORDS = {
    "rs": "INR", "inr": "INR", "usd": "USD", "cny": "CNY",
    "rmb": "CNY", "eur": "EUR", "gbp": "GBP",
}

def _to_decimal(num: str):
    try:
        num = num.strip()
        # "4,50" is a decimal comma (thousands groups always have 3 digits).
        if re.fullmatch(r"\d+,\d{1,2}", num):
            return Decimal(num.replace(",", "."))
        return Decimal(num.replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None

def dec_num(d):
    if not isinstance(d, Decimal):
        return None
    return int(d) if d == d.to_integral_value() else float(d)

_CURRENCY_MARKER_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True))
)
_DISCOUNT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*off\b", re.I)
# A two-decimal price run straight into its discount badge ("256.16" + "10% off"
# read back as "256.1610% off" when sibling nodes were concatenated).
_GLUED_DISCOUNT_RE = re.compile(r"(\d[\d,]*\.\d{2})(\d{1,2})(\s*%\s*off\b)", re.I)
_RANGE_END_RE = re.compile(r"(?:[-–—]|\bto)\s*$", re.I)


def _price_segments(text: str) -> list[str]:
    """Split a card price at each currency symbol, keeping "$1.20-$2.50" (a
    range that repeats the symbol) together as one segment."""
    starts = [m.start() for m in _CURRENCY_MARKER_RE.finditer(text)]
    if len(starts) < 2:
        return [text]
    cuts = [0 if starts[0] == 0 else starts[0]] + starts[1:] + [len(text)]
    segs: list[str] = []
    for a, b in zip(cuts, cuts[1:]):
        seg = text[a:b]
        if segs and _RANGE_END_RE.search(segs[-1].strip()):
            segs[-1] += seg
        else:
            segs.append(seg)
    return segs


def _range_of(text: str):
    nums = [d for d in (_to_decimal(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)) if d is not None]
    if not nums:
        return None, None, False
    if len(nums) >= 2 and re.search(r"[-–—]|\bto\b", text):
        return min(nums), max(nums), True
    return nums[0], nums[0], False


def parse_price(raw) -> dict:
    """Card price text -> current price range, plus the struck-through original
    price and the "N% off" badge when the card shows them
    (``"₹196.39-230.55 ₹218.22-256.16 10% off"``). Numbers that belong to the
    original price or the discount are never mixed into the current range."""
    out = {
        "price_raw": raw if raw not in (None, "", "N/A") else None,
        "price_min": None, "price_max": None, "currency": None, "price_unit": None,
        "original_min": None, "original_max": None, "discount_percentage": None,
        "price_extraction_status": "missing",
    }
    if not raw or str(raw).strip() in ("", "N/A"):
        return out
    text = _GLUED_DISCOUNT_RE.sub(r"\1 \2\3", str(raw).strip())

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

    dm = _DISCOUNT_RE.search(text)
    if dm:
        pct = float(dm.group(1))
        if 0 < pct < 100:
            out["discount_percentage"] = int(pct) if pct == int(pct) else pct
        text = (text[:dm.start()] + " " + text[dm.end():]).strip()

    segs = _price_segments(text)
    lo, hi, is_range = _range_of(segs[0])
    if lo is None:
        out["price_extraction_status"] = "unparseable"
        return out
    out["price_min"], out["price_max"] = dec_num(lo), dec_num(hi)
    out["price_extraction_status"] = "range_parsed" if is_range else "parsed"

    if len(segs) >= 2:
        olo, ohi, _ = _range_of(segs[1])
        # Only a higher price can be the struck-through one.
        if olo is not None and olo >= lo and ohi >= hi:
            out["original_min"], out["original_max"] = dec_num(olo), dec_num(ohi)
    return out

_UNIT_SINGULAR = {
    "pieces": "piece", "pcs": "piece", "pc": "piece", "sets": "set", "units": "unit",
    "kilograms": "kilogram", "kgs": "kilogram", "kg": "kilogram", "tons": "ton",
    "tonnes": "tonne", "meters": "meter", "metres": "meter", "pairs": "pair",
    "boxes": "box", "bags": "bag", "rolls": "roll", "cartons": "carton",
    "dozens": "dozen", "acres": "acre", "liters": "liter", "litres": "liter",
}

def parse_moq(raw) -> dict:
    out = {
        "moq_raw": raw if raw not in (None, "", "N/A") else None,
        "moq_value": None, "moq_unit": None, "moq_extraction_status": "missing",
    }
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
    out["moq_value"] = dec_num(val)
    unit = m.group(2).lower()
    out["moq_unit"] = _UNIT_SINGULAR.get(unit, unit)
    out["moq_extraction_status"] = "parsed"
    return out

_ICON_RE = re.compile(
    r"\.svg(\?|$)|/flags?/|/common/|/mobile/|tps-\d{1,3}-\d{1,3}\.|"
    r"-55-tps-|-56-tps-|icon|sprite|placeholder|avatar",
    re.I,
)

def _is_icon(u: str) -> bool:
    return not u or not u.startswith("http") or bool(_ICON_RE.search(u))

def classify_images(card_images, product_media, supplier_logo) -> dict:
    pm_imgs = (product_media or {}).get("images") or []
    product_imgs: list[str] = []
    seen: set[str] = set()
    for u in pm_imgs:
        if u and u not in seen and not _is_icon(u):
            seen.add(u)
            product_imgs.append(u)
    if not product_imgs:
        for u in card_images or []:
            if u and u not in seen and not _is_icon(u):
                seen.add(u)
                product_imgs.append(u)
    other: list[str] = []
    for u in card_images or []:
        if u and u not in seen and not _is_icon(u) and u != supplier_logo:
            seen.add(u)
            other.append(u)
    if product_imgs:
        status = "classified"
    elif card_images:
        status = "icons_only"
    else:
        status = "missing"
    return {
        "main_image_url": product_imgs[0] if product_imgs else None,
        "product_image_urls": product_imgs,
        "supplier_logo_url": supplier_logo or None,
        "other_media_urls": other,
        "image_extraction_status": status,
    }

_MODEL_JUNK = {
    "number", "no", "no.", "num", "num.", "model", "model number",
    "n/a", "na", "-", "null", "none", "",
}
_MODEL_LABELS = ("model number", "model no.", "model no", "model")
# A label is a model-number label when it contains the word "model" (optionally
# followed by "no"/"number"/"#"). Matched by regex/substring rather than an exact
# equality to _MODEL_LABELS so "Model No.", "Model Number", "Phone Model", etc.
# all qualify.
_MODEL_LABEL_RE = re.compile(r"\bmodel(\s*(no\.?|number|#))?\b", re.I)
_CPU_LIKE_RE = re.compile(
    r"^(i[3579][\s-]?\d{2,5}[a-z]*|celeron|pentium|atom|ryzen|athlon|"
    r"core\s*(i|ultra|m)|xeon|snapdragon|mediatek|mt\d|"
    r"n\d{3,4}[a-z]*|j\d{3,4}[a-z]*|a\d{3,4}[a-z]*)$",
    re.I,
)
_GENERIC_BRANDS = {
    "oem", "odm", "oem/odm", "none", "n/a", "na", "null", "-", "generic",
    "no brand", "nobrand", "unbranded", "other", "others", "customized", "custom", "",
}

def sanitize_model_number(v):
    if v in (None, "", "N/A"):
        return None
    s = str(v).strip()
    if s.lower() in _MODEL_JUNK:
        return None
    return s

def _norm_ident(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()

def _is_model_label(name) -> bool:
    return bool(_MODEL_LABEL_RE.search(_norm_ident(name)))

def _cpu_spec_values(specs: dict) -> list[str]:
    out = []
    for k, v in (specs or {}).items():
        kl = str(k).lower()
        if ("processor" in kl or "cpu" in kl) and v not in (None, "", "N/A"):
            out.append(str(v))
    return out

def _model_candidates(ex: dict) -> list[tuple[str, str]]:
    cands: list[tuple[str, str]] = []
    # The scraper already extracts a model number (scripts.py: variantModel or a
    # "model number"/"model" attribute pick); use it as the authoritative
    # candidate instead of discarding it.
    pre = ex.get("model_number")
    if pre not in (None, "", "N/A"):
        cands.append(("extracted", str(pre).strip()))
    for v in ex.get("variants") or []:
        if _is_model_label(v.get("name")):
            vals = [x for x in (v.get("values") or []) if x]
            if vals:
                cands.append(("variant", str(vals[0]).strip()))
                break
    for k, val in (ex.get("specifications") or {}).items():
        if _is_model_label(k) and val not in (None, "", "N/A"):
            cands.append(("specification", str(val).strip()))
            break
    return cands

def _is_cpu_value(model: str, cpu_values: list[str]) -> str | None:
    """Flags a model code ONLY when it is IDENTICAL to a processor/CPU spec value
    on the page. A merely CPU-shaped code (one that looks like a chip name but
    does not equal any CPU spec) is a legitimate model number and is kept."""
    nm = _norm_ident(model)
    if not nm:
        return None
    for cv in cpu_values:
        if _norm_ident(cv) == nm:
            return cv
    return None

def validate_model_number(ex: dict) -> dict:
    specs = ex.get("specifications") or {}
    cpu_values = _cpu_spec_values(specs)
    raw_cands = _model_candidates(ex)

    cleaned: list[tuple[str, str]] = []
    for src, val in raw_cands:
        s = sanitize_model_number(val)
        if s is not None:
            cleaned.append((src, s))

    if not cleaned:
        return {"value": None, "status": "not_provided", "source": None, "raw": None,
                "candidates": [], "reason": "no model-number attribute on page",
                "collides_with": None, "confidence": None}

    all_values = [c for _, c in cleaned]
    collisions = [(src, val, _is_cpu_value(val, cpu_values)) for src, val in cleaned]
    non_cpu = [(src, val) for (src, val, col) in collisions if col is None]

    if not non_cpu:
        first_src, first_val, first_col = collisions[0]
        return {"value": None, "status": "invalid", "source": first_src, "raw": first_val,
                "candidates": all_values,
                "reason": f"candidate {first_val!r} equals processor/CPU value",
                "collides_with": first_col, "confidence": None}

    top_src, top_val, top_col = collisions[0]
    if top_col is not None:
        return {"value": None, "status": "not_verified", "source": top_src, "raw": top_val,
                "candidates": all_values,
                "reason": f"authoritative model source was a CPU value ({top_val!r})",
                "collides_with": top_col, "confidence": None}

    distinct = {_norm_ident(v) for _, v in non_cpu}
    status = "conflicting" if len(distinct) > 1 else "available"
    if status == "conflicting":
        confidence = "low"
    else:
        confidence = "high" if top_src == "variant" else "medium"
    reason = (f"multiple distinct model candidates {[v for _, v in non_cpu]}"
              if status == "conflicting" else None)
    return {"value": top_val, "status": status, "source": top_src, "raw": top_val,
            "candidates": all_values, "reason": reason, "collides_with": None,
            "confidence": confidence}

def sanitize_brand(v) -> dict:
    if v in (None, "", "N/A"):
        return {"value": None, "status": "missing_on_source", "raw": None,
                "reason": "no brand attribute on page"}
    s = str(v).strip()
    if s.lower() in _GENERIC_BRANDS:
        return {"value": None, "status": "not_verified", "raw": s,
                "reason": f"generic value {s!r}; consumer brand not established"}
    return {"value": s, "status": "available", "raw": s, "reason": None}

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
_REPLY_RE = re.compile(r"(?i)supplier'?s?\s*repl(?:y|ies)\s*:?\s*")

def split_supplier_reply(text):
    if not isinstance(text, str):
        return text, None
    m = _REPLY_RE.search(text)
    if not m:
        return text, None
    customer = text[: m.start()].strip() or "N/A"
    reply = text[m.end():].strip() or None
    return customer, reply

def _looks_like_ui(text) -> bool:
    t = str(text or "").strip()
    if not t or t == "N/A":
        return False
    return bool(_UI_NOISE_RE.match(t)) or bool(_TAGBAR_RE.match(t))

def parse_stars(raw):
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

def dedupe(seq) -> list:
    seen, out = set(), []
    for x in seq or []:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def validate_review(item: dict) -> dict:
    warnings: list[str] = []
    reviewer = item.get("reviewer", "N/A")
    country = item.get("country", "N/A")
    rating = parse_stars(
        item.get("rating") if item.get("rating") is not None else item.get("stars")
    )

    ctext, _ = split_supplier_reply(item.get("text", "N/A"))
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
    has_content = (
        genuine_text
        or (rating is not None)
        or (media_present and clean_reviewer is not None)
    )
    return {"valid": bool(has_content), "rating": rating, "warnings": warnings,
            "clean_reviewer": clean_reviewer, "clean_country": clean_country}

def review_quality(block: dict | None, tab_count: int, complete: bool = False) -> dict:
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
        it["country"] = res["clean_country"] or "N/A"
        it["rating"] = res["rating"]
        it["valid"] = True
        clean.append(it)

    extracted = valid_n = len(clean)
    missing_ratings = sum(1 for it in clean if it.get("rating") is None)

    if complete:
        displayed = valid_n
        status = "available" if valid_n > 0 else "no_reviews_reported"
    else:
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

def normalize_native_item(it: dict) -> dict:
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
        is_vid = (
            isinstance(mo, dict)
            and (mo.get("videoUrl") or str(mo.get("type", "")).lower() == "video")
        ) or bool(re.search(r"\.(mp4|webm|mov|m3u8)(\?|$)", u, re.I))
        (vids if is_vid else imgs).append(u)

    text = str(pick("content", "feedback", "feedbackContent", "reviewContent", "comment", "translatedContent"))
    reply = pick("sellerReply", "supplierReply", "replyContent", "reply",
                 "feedbackReply", "companyReply", default=None)
    if isinstance(reply, dict):
        reply = reply.get("content") or reply.get("text") or reply.get("reply")
    if reply is None:
        text, reply = split_supplier_reply(text)
    native_id = pick("feedbackId", "reviewId", "evaluateId", "id", default=None)
    return {
        # Alibaba's own review id when the feedback API exposes one; the mapper
        # falls back to a content-derived id otherwise.
        "review_id": str(native_id) if native_id is not None else None,
        "reviewer": str(pick("buyerName", "memberName", "displayName", "nickName", "buyerNickName", "reviewerName")),
        "country": str(pick("countryName", "country", "buyerCountry", "countryCode")),
        "date": str(pick("gmtCreate", "postDate", "feedbackDate", "createTime", "date", "orderCompleteTime")),
        "stars": str(star) if star is not None else "N/A",
        "text": text,
        "supplier_reply": (str(reply).strip() or None) if reply else None,
        "model_number": sanitize_model_number(pick("sku", "goodsSku", "skuInfo", "specInfo")),
        "verified_purchase": bool(it.get("verifiedPurchase") or it.get("isVerified") or it.get("hasOrder")),
        "repeat_buyer": bool(it.get("repeatBuyer") or it.get("isRepeat")),
        "media": {"images": dedupe(imgs), "videos": dedupe(vids)},
    }


# ── Amazon-aligned helpers (pure; used by engine, mapper and adapter) ─────────
# Same rule as the Amazon parsers: never fabricate. Anything the page does not
# expose stays None / empty.

def _blank(v) -> bool:
    return v is None or str(v).strip().lower() in _BAD_NAMES


def to_int(raw) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    m = re.search(r"\d[\d,]*", str(raw))
    return int(m.group().replace(",", "")) if m else None


def to_number(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(raw))
    return float(m.group().replace(",", "")) if m else None


def parse_rating_value(raw) -> float | None:
    """Average rating as a float on the 1-5 scale (4.8 stays 4.8; ``parse_stars``
    rounds, which is right for single reviews but not for averages)."""
    if raw is None or isinstance(raw, bool) or str(raw).strip() in ("", "N/A"):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        m = re.search(r"\d+(?:\.\d+)?", str(raw))
        if not m:
            return None
        v = float(m.group())
    return round(v, 1) if 0 < v <= 5 else None


def video_urls(videos) -> list[str]:
    """Detail-page videos are dicts (``{"url": ..., "cover_url": ...}``) while
    search-card videos are plain URLs; return URL strings only."""
    out = []
    for v in videos or []:
        u = v.get("url") if isinstance(v, dict) else v
        if isinstance(u, str):
            u = "https:" + u if u.startswith("//") else u
            if u.startswith("http"):
                out.append(u)
    return dedupe(out)


def currency_from_pattern(pattern) -> str | None:
    """ISO code from Alibaba's ``currencyRule.currencyPattern`` (e.g. ``US$#``)."""
    if not pattern:
        return None
    for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in str(pattern):
            return _CURRENCY_SYMBOLS[sym]
    return None


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve_price(price_range_raw, ex: dict | None) -> dict:
    """Product price in the display currency: the card's price text, refined by
    the detail page's quantity tiers when present. ``usd_min``/``usd_max`` are
    Alibaba's own US-dollar figures (``dollarPrice``), never a conversion."""
    ex = ex or {}
    price = parse_price(price_range_raw)
    tiers = [t for t in ex.get("price_tiers") or [] if isinstance(t, dict) and _is_number(t.get("price"))]
    tier_prices = [t["price"] for t in tiers]
    rng = ex.get("price_range_detail") or {}
    price["price_source"] = "card" if price["price_min"] is not None else "none"
    if tier_prices:
        # Each ladder entry can carry BOTH its regular price AND (when a
        # listing-wide discount is running) a lower promotion_price for the
        # SAME quantity band (confirmed live: a tier shown as "2-99 pieces"
        # displayed a struck ₹928.87 next to a current ₹715.61 - both read
        # from the one ladder entry). When every tier reports one, the
        # promotional ladder is what a buyer actually pays, so IT becomes
        # price_min/price_max, with the regular ladder kept as the real,
        # same-tier-paired original/struck-through range - never the
        # search-card's own single-tier badge, which describes a different
        # quantity point and would mix two unrelated prices into one "range".
        promo_tier_prices = [t.get("promotion_price") for t in tiers if _is_number(t.get("promotion_price"))]
        has_tier_promo = (len(promo_tier_prices) == len(tier_prices)
                          and any(p != r for p, r in zip(promo_tier_prices, tier_prices)))
        if has_tier_promo:
            price["original_min"], price["original_max"] = min(tier_prices), max(tier_prices)
            price["price_min"], price["price_max"] = min(promo_tier_prices), max(promo_tier_prices)
            price["discount_percentage"] = None  # recomputed below from this real pair, never the card badge
        else:
            price["price_min"], price["price_max"] = min(tier_prices), max(tier_prices)
            # No per-tier promotion data: a card-level struck-through price /
            # "N% off" badge describes a different quantity point (the card's
            # own single min-order tier), so it must not be kept as if it
            # were this ladder's own original price (confirmed live: doing so
            # produced a nonsense "discount" between two unrelated tiers).
            price["original_min"] = price["original_max"] = None
            price["discount_percentage"] = None
        price["price_source"] = "price_tiers"
        if not price.get("currency"):
            price["currency"] = currency_from_pattern(ex.get("price_currency_pattern"))
    elif _is_number(rng.get("min")) or _is_number(rng.get("promo_min")):
        # Range-priced product: the detail page's own figures replace the card
        # text. During a promotion the promotional range is what the buyer
        # pays and the regular range is the struck-through original.
        promo = _is_number(rng.get("promo_min"))
        lo = rng["promo_min"] if promo else rng["min"]
        hi = rng.get("promo_max") if promo else rng.get("max")
        price["price_min"], price["price_max"] = lo, hi if _is_number(hi) else lo
        if promo and _is_number(rng.get("min")):
            price["original_min"] = rng["min"]
            price["original_max"] = rng["max"] if _is_number(rng.get("max")) else rng["min"]
        elif not promo and not ex.get("promotion"):
            # The detail page shows no promotion: drop any card-level
            # struck-through price / badge rather than mixing the two sources.
            price["original_min"] = price["original_max"] = None
            price["discount_percentage"] = None
        price["price_source"] = "detail_range"
        price["currency"] = currency_from_pattern(ex.get("price_currency_pattern")) or price.get("currency")
    promotion = ex.get("promotion") or {}
    if price.get("discount_percentage") is None:
        rate = to_number(promotion.get("discount_rate"))
        if rate is not None and 0 < rate < 100:
            price["discount_percentage"] = int(rate) if rate == int(rate) else rate
    price["usd_min"] = ex.get("price_usd_min") if _is_number(ex.get("price_usd_min")) else None
    price["usd_max"] = ex.get("price_usd_max") if _is_number(ex.get("price_usd_max")) else None
    price["price_unit"] = price.get("price_unit") or ex.get("price_unit")
    return price


def spec_get(specs: dict, *names: str):
    """Case-insensitive exact key lookup; first non-blank value wins."""
    lower = {str(k).strip().lower(): v for k, v in (specs or {}).items()}
    for n in names:
        v = lower.get(n.lower())
        if not _blank(v):
            return str(v).strip()
    return None


def canonical_product_url(url: str | None) -> str | None:
    """Product page URL without tracking query / fragment (``?priceId=...``)."""
    if not url:
        return None
    p = urllib.parse.urlsplit(str(url).strip())
    return urllib.parse.urlunsplit((p.scheme or "https", p.netloc, p.path, "", "")) if p.netloc else None


def _id_str(v) -> str | None:
    return None if v is None or isinstance(v, bool) or str(v).strip() == "" else str(v).strip()


def variant_options(variants, sku_map=None, product_id: str | None = None,
                    product_url: str | None = None) -> dict[str, list[dict]]:
    """SKU attributes in the Amazon/Flipkart ``variants`` shape:
    ``{"Color": [{"option": "Black", "url": ..., "product_id": ..., ...}]}``.

    On Amazon every variant is its own product (own ASIN and /dp/ page). On
    Alibaba every option is a SKU of the SAME listing: choosing it does not
    change the page URL, so ``product_id`` / ``url`` are the listing the
    option is bought on. The option's own identity is kept alongside:
    ``sku_ids`` (the purchasable SKUs that include it, from the page's
    ``skuInfoMap``), ``attribute_id`` / ``value_id`` and its swatch ``image``."""
    url = canonical_product_url(product_url)
    pid = _id_str(product_id)
    combos = []
    for e in sku_map or []:
        if isinstance(e, dict) and _id_str(e.get("sku_id")) and e.get("combo"):
            tokens = {t.strip() for t in str(e["combo"]).split(";") if t.strip()}
            combos.append((tokens, _id_str(e["sku_id"])))

    out: dict[str, list[dict]] = {}
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        name = re.sub(r"\s+", " ", str(v.get("name") or "")).strip()
        attr_id = _id_str(v.get("id"))
        detailed = {}
        for o in v.get("options") or []:
            if isinstance(o, dict) and isinstance(o.get("name"), str):
                detailed.setdefault(re.sub(r"\s+", " ", o["name"]).strip(), o)
        values = dedupe(re.sub(r"\s+", " ", x).strip() for x in v.get("values") or [] if isinstance(x, str))
        values = values or list(detailed)
        if not (name and values):
            continue
        opts = out.setdefault(name, [])
        for val in values:
            if any(o["option"] == val for o in opts):
                continue
            d = detailed.get(val) or {}
            value_id = _id_str(d.get("value_id"))
            token = f"{attr_id}:{value_id}" if attr_id and value_id else None
            sku_ids = dedupe(sku for tokens, sku in combos if token and token in tokens)
            opts.append({
                "option": val, "url": url, "product_id": pid,
                "sku_ids": sku_ids, "attribute_id": attr_id, "value_id": value_id,
                "image": d.get("image") or None,
            })
    return out


_CRUMB_NOISE = {"home", "all categories", "all industries", "alibaba.com", "alibaba", "products"}


def clean_breadcrumbs(path) -> list[str]:
    """Category path from the detail page's SEO breadcrumb, without site-level
    entries such as "Home" / "All Industries"."""
    out: list[str] = []
    for c in path or []:
        c = re.sub(r"\s+", " ", str(c or "")).strip()
        if c and c.lower() not in _CRUMB_NOISE and c not in out:
            out.append(c)
    return out


_MFG_SPEC_KEYS = {
    "manufactured_by": ("manufacturer", "manufacturer name", "manufactured by"),
    "country_of_origin": ("place of origin", "country of origin", "origin"),
    "brand_owner": ("brand owner",),
}


def extract_manufacturer(ex: dict | None, brand: str | None = None) -> dict | None:
    """Manufacturer details in the Amazon/Flipkart ``manufacturer`` shape, read
    only from the product's own attributes (the supplier's business type is
    not taken as proof that it made this product)."""
    ex = ex or {}
    specs = ex.get("specifications") or {}
    out = {}
    for key, names in _MFG_SPEC_KEYS.items():
        v = spec_get(specs, *names)
        if key == "country_of_origin" and not v and not _blank(ex.get("place_of_origin")):
            v = str(ex["place_of_origin"]).strip()
        if v:
            out[key] = v
    if not out:
        return None
    name = out.get("manufactured_by") or brand
    if name:
        out["name"] = name
    return out


def wholesale_terms(ex: dict | None, card_moq) -> dict | None:
    """Alibaba's B2B purchase terms (MOQ, quantity price tiers, lead time,
    customization, shipping origin, after-sales / returns) for
    ``commercial.offers_detail``. None when the page shows none of them."""
    ex = ex or {}
    moq = parse_moq(card_moq)
    if _is_number(ex.get("moq_value")):
        moq["moq_value"] = ex["moq_value"]
        moq["moq_extraction_status"] = "parsed"
        if not moq["moq_unit"] and ex.get("price_unit"):
            moq["moq_unit"] = str(ex["price_unit"]).lower()
    custom = ex.get("customization") or {}
    terms = {
        "moq": moq if moq["moq_value"] is not None or moq["moq_raw"] else None,
        "price_tiers": [t for t in ex.get("price_tiers") or [] if isinstance(t, dict)],
        "lead_time": [t for t in ex.get("lead_time") or [] if isinstance(t, dict)],
        "box_moq": ex.get("box_moq"),
        "shipping_origin": ex.get("shipping_origin"),
        "customization": custom if (custom.get("light") or custom.get("fast") or custom.get("items")) else None,
        "after_sales_service": None if _blank(ex.get("after_sales_service")) else ex["after_sales_service"],
        "returns": None if _blank(ex.get("returns")) else ex["returns"],
    }
    terms = {k: v for k, v in terms.items() if v not in (None, "", [], {})}
    return terms or None


def delivery_info(ex: dict | None) -> dict | None:
    """Delivery in the shared ``commercial.delivery`` shape, from the detail
    page's inventory data (``window.detailData.globalData.inventory``). Alibaba
    is a B2B wholesale marketplace: no delivery charge or ETA is shown for a
    listing, but the warehouse's dispatch place(s) (e.g. "China") and the
    destination country the page resolved shipping to (e.g. "IN") genuinely
    are, so those are carried through as extra fields. None when the page
    exposed neither."""
    inv = (ex or {}).get("inventory") or {}
    dispatch = dedupe(p for p in inv.get("dispatch_places") or [] if not _blank(p))
    ships_to = inv.get("ships_to")
    if isinstance(ships_to, (list, tuple)):
        ships_to = dedupe(s for s in ships_to if not _blank(s)) or None
    elif _blank(ships_to):
        ships_to = None
    if not dispatch and not ships_to:
        return None
    out: dict = {}
    if dispatch:
        out["dispatch_places"] = dispatch
    if ships_to:
        out["ships_to"] = ships_to
    return out


# Alibaba reports untracked / unlimited stock as this count.
_UNTRACKED_STOCK = 9_999_999


# ── independent per-SKU extraction ──────────────────────────────────────────
# Live investigation (Nov 2026, two real multi-attribute listings - a phone
# case with colour + "compatible model", and a search for an apparel listing
# that found none live at the time): Alibaba's own window.detailData has NO
# per-SKU price field anywhere (skuInfoMap entries are only {"id": sku_id});
# the page's price is a single quantity-order-tier ladder
# (product.price.productLadderPrices) for the WHOLE listing. Clicking a
# different colour/spec swatch does not change the displayed price and fires
# no SKU- or price-specific network request (only generic page/chat/
# recommendation widgets were observed). So, unlike Amazon, there is no
# per-SKU price to capture and none is fabricated here. What genuinely DOES
# vary per SKU, and is already present on the one page already scraped (no
# extra request needed): each SKU's own stock (inventory.repoSkuInventory /
# skuInventory, keyed by the same sku id skuInfoMap gives - see scripts.py's
# sku_stock_map) and, for an IMAGE-type attribute (e.g. colour), that value's
# own swatch image.

def sku_availability(stock) -> dict:
    """Per-SKU stock status, same scale/semantics as availability() above but
    for one SKU's own inventory count instead of the whole listing's total."""
    if not _is_number(stock) or stock < 0:
        return {"status": None, "stock_quantity": None}
    if stock == 0:
        return {"status": "out_of_stock", "stock_quantity": 0}
    tracked = stock < _UNTRACKED_STOCK
    return {"status": "in_stock", "stock_quantity": int(stock) if tracked else None}


def sku_variant_combinations(ex: dict | None) -> list[dict]:
    """Every purchasable SKU of this listing (``window.detailData``'s
    ``sku.skuAttrs`` + ``skuInfoMap``, captured by scripts.py's SELLER_JS into
    ``ex["variants"]``/``ex["sku_map"]``), each with its own full attribute
    combination, swatch image (when the attribute is an image-type one, e.g.
    colour) and stock (from ``ex["inventory"]["sku_stock_map"]``). Unlike
    Amazon's child ASINs, an Alibaba SKU is not a separate page: every SKU here
    is read from data already on the ONE product page already scraped - no
    extra request, no extra concurrency to bound. Returns [] when the page
    exposed no SKU attribute data (not every Alibaba listing has SKU
    attributes at all - a single-configuration product has none)."""
    ex = ex or {}
    attrs = ex.get("variants") or []
    sku_map = ex.get("sku_map") or []
    if not attrs or not sku_map:
        return []
    stock_map = (ex.get("inventory") or {}).get("sku_stock_map") or {}

    attr_names: dict[str, str] = {}
    value_index: dict[str, dict[str, dict]] = {}
    for a in attrs:
        if not isinstance(a, dict):
            continue
        aid = _id_str(a.get("id"))
        name = re.sub(r"\s+", " ", str(a.get("name") or "")).strip()
        if not aid or not name:
            continue
        attr_names[aid] = name
        vi = value_index.setdefault(aid, {})
        for o in a.get("options") or []:
            if not isinstance(o, dict) or not o.get("name"):
                continue
            vid = _id_str(o.get("value_id"))
            if vid:
                vi[vid] = {"name": re.sub(r"\s+", " ", str(o["name"])).strip(), "image": o.get("image")}

    out: list[dict] = []
    for entry in sku_map:
        if not isinstance(entry, dict):
            continue
        sku_id = _id_str(entry.get("sku_id"))
        combo = entry.get("combo")
        if not sku_id or not combo:
            continue
        attributes: dict[str, str] = {}
        image = None
        for token in str(combo).split(";"):
            token = token.strip()
            if not token or ":" not in token:
                continue
            aid, vid = token.split(":", 1)
            name = attr_names.get(aid)
            val = value_index.get(aid, {}).get(vid)
            if name and val:
                attributes[name] = val["name"]
                if not image and val.get("image"):
                    image = val["image"]
        if not attributes:
            continue
        stock = stock_map.get(sku_id)
        out.append({
            "platform_variant_id": sku_id,
            "attributes": attributes,
            "image": image,
            "stock_quantity": stock if _is_number(stock) else None,
        })
    return out


def parse_listing_variant_text(text, attribute_names) -> dict[str, str]:
    """"Label: Value" pairs from a review's own SKU/model text, recognizing
    ONLY this listing's own SKU attribute names as labels - Alibaba has no
    fixed, platform-wide variant vocabulary the way Amazon does (every
    listing names its own attributes, e.g. "color", "compatible model"), so a
    generic known-label list cannot be reused across listings. Returns {}
    when the text contains none of this listing's own attribute names as a
    label - never guessed from unrelated free text."""
    names = sorted({str(n).strip() for n in (attribute_names or []) if str(n).strip()}, key=len, reverse=True)
    if _blank(text) or not names:
        return {}
    pattern = re.compile(r"(?i)\b(" + "|".join(re.escape(n) for n in names) + r")\s*:\s*")
    s = str(text)
    matches = list(pattern.finditer(s))
    if not matches:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        label = m.group(1).strip()
        canon = next((n for n in names if n.lower() == label.lower()), label)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(s)
        value = re.sub(r"\s+", " ", s[m.end():end].strip(" |;,")).strip()
        if value:
            out[canon] = value
    return out


def availability(ex: dict | None) -> dict:
    """Stock status in Amazon's ``availability`` shape, from the detail page's
    per-SKU inventory. The quantity is the total over SKUs, and only when every
    SKU reports a real count (not Alibaba's "untracked" placeholder)."""
    inv = (ex or {}).get("inventory") or {}
    stock = [n for n in inv.get("sku_stock") or [] if _is_number(n) and n >= 0]
    if not stock:
        return {"status": None, "stock_quantity": None}
    if not any(n > 0 for n in stock):
        return {"status": "out_of_stock", "stock_quantity": 0}
    tracked = all(n < _UNTRACKED_STOCK for n in stock)
    return {"status": "in_stock", "stock_quantity": int(sum(stock)) if tracked else None}


def promotion_info(ex: dict | None, price: dict, coupon: dict | None = None) -> dict:
    """Promotion in Amazon's ``promotion`` shape: the detail page's running
    discount when present, else the card's struck-through price / badge, plus
    the seller coupon banner (``coupon``, the engine's ``COUPON_JS`` capture)
    when the page shows one. Alibaba auto-applies a seller coupon at checkout
    and does not print a redeemable code for the buyer, so ``coupon_code``
    stays unset unless the banner itself ever shows one."""
    promo = (ex or {}).get("promotion") or {}
    has_discount = price.get("discount_percentage") is not None or price.get("original_min") is not None
    if promo:
        name = promo.get("text")
        if not name and promo.get("discount_rate") not in (None, ""):
            name = f"{promo['discount_rate']}% off"
        out = {"is_promotional": True, "promotion_type": "discount", "promotion_name": name}
    else:
        out = {"is_promotional": has_discount, "promotion_type": "discount" if has_discount else None,
               "promotion_name": None}
    if coupon:
        out["is_promotional"] = True
        out["promotion_type"] = out["promotion_type"] or "coupon"
        out["promotion_name"] = out["promotion_name"] or coupon.get("text")
        out["coupon_available"] = True
        if not _blank(coupon.get("code")):
            out["coupon_code"] = coupon["code"]
    else:
        out["coupon_available"] = None
    return out


_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁠﻿]")


def _clean_desc_text(s) -> str | None:
    if _blank(s):
        return None
    s = re.sub(r"\s+", " ", _INVISIBLE_RE.sub("", str(s))).strip()
    return s or None


def description_block(raw: dict | None) -> dict:
    """Supplier description (Amazon: ``description`` + ``features``) from the
    engine's description capture. The description is the iframe document's
    text, else the structured layout's text blocks; highlights are the
    "Highlights at a glance" bullets. Like Amazon, a description shorter than
    30 characters is treated as absent and long ones are capped at 5000."""
    raw = raw or {}
    highlights = dedupe(t for t in (_clean_desc_text(h) for h in raw.get("highlights") or []) if t)
    text = _clean_desc_text(raw.get("iframe_text"))
    source = "iframe" if text else None
    if not text:
        text = _clean_desc_text(" ".join(p for p in raw.get("paragraphs") or [] if isinstance(p, str)))
        source = "structured" if text else None
    if text and len(text) < 30:
        text, source = None, None
    return {
        "description": text[:5000] if text else None,
        "highlights": highlights,
        "source": source or ("structured" if highlights else None),
    }


_DETAIL_STOP_REASONS = {
    "failed": "detail page failed to load",
    "skipped_run_stopped": "detail page not visited (run stopped early)",
}
# Pagination ended because the source had no further page / nothing new.
_NATURAL_END = ("last review page", "no new reviews", "collected all reported", "next_disabled",
                "no_new_reviews", "listing exhausted")


def review_coverage(available, extracted, stop: str | None, failed: bool = False) -> dict:
    """Reported review count vs. reviews extracted (as on Amazon).
    ``complete`` is None when either count is unknown."""
    out = {"reviews_available": available, "reviews_extracted": extracted,
           "coverage_percent": round(min(extracted, available) / available * 100, 1)
           if available and extracted is not None else None,
           "complete": None, "incomplete_reason": None}
    if available is None or extracted is None:
        return out
    out["complete"] = extracted >= available
    if out["complete"]:
        return out
    stop = stop or ""
    if failed:
        out["incomplete_reason"] = "detail page failed to load"
        return out
    # A natural end to pagination (no further page / nothing new -- next_disabled,
    # no_new_reviews, last page, ...) means the source listed every review it
    # exposes. Alibaba's displayed count can exceed what its pages actually list
    # (store feedback is text-only and never reaches the product-tab total), so a
    # natural end is complete coverage, not an incomplete collection.
    if any(s in stop for s in _NATURAL_END):
        out["complete"] = True
        return out
    out["incomplete_reason"] = f"review pagination stopped early ({stop or 'unknown'})"
    return out


def build_review_summary(detail: dict | None, card_rating=None, product_url: str | None = None,
                         detail_status: str | None = None) -> dict:
    """Review aggregate in Amazon's ``review_summary`` shape (same keys, same
    meaning), followed by Alibaba-only keys: the review source used and the
    supplier-level (store) review aggregate. Values the page does not show
    stay None / empty. ``detail_status`` explains a missing review collection
    for products kept with search-card data only."""
    detail = detail or {}
    prns = detail.get("product_review_summary") or {}
    pr = detail.get("product_reviews") or {}
    sr = detail.get("store_reviews") or {}
    srs = detail.get("store_review_summary") or {}
    seller = detail.get("seller") or {}

    total = to_int(prns.get("total_review_count"))
    if total is None and pr.get("tab_count"):
        total = to_int(pr.get("tab_count"))
    avg = parse_rating_value(prns.get("average_star"))
    if avg is None:
        avg = parse_rating_value(card_rating)

    if pr:
        stop = pr.get("stop_reason") or "first review page only"
    elif detail_status in _DETAIL_STOP_REASONS:
        stop = _DETAIL_STOP_REASONS[detail_status]
    else:
        stop = "product has no reviews" if total == 0 else "no product reviews found on page"

    store = None
    if sr or srs:
        store = {
            "average_rating": parse_rating_value(srs.get("averageStar")),
            # The tab's displayed total ("Store reviews (944)") is the review count;
            # the rating-scatter sum is only a count of rated feedback.
            "total_reviews": to_int(sr.get("tab_count")) or to_int(srs.get("total_reviews")),
            "sub_ratings": srs.get("sub_ratings") or {},
            "feedback_url": seller.get("feedbackUrl"),
            "pages_scraped": sr.get("feedback_pages") or sr.get("pages_scraped") or (1 if sr else 0),
            "stop_reason": sr.get("feedback_termination") or sr.get("stop_reason"),
            "complete": sr.get("feedback_complete"),
            "review_source": sr.get("_source"),
        }
        sc = review_coverage(store["total_reviews"],
                             sr.get("extracted_before_filter", len(sr.get("reviews") or [])) if sr else None,
                             store["stop_reason"])
        store.update(reviews_extracted=sc["reviews_extracted"], coverage_percent=sc["coverage_percent"],
                     coverage_complete=sc["complete"], incomplete_reason=sc["incomplete_reason"])

    product_extracted = pr.get("extracted_before_filter", len(pr.get("reviews") or [])) if pr else \
        (0 if detail and detail_status is None else None)
    coverage = review_coverage(total, product_extracted, stop,
                               failed=detail_status == "failed" and not pr)

    summary = {
        "total_ratings": None,  # Alibaba reports a review count, not a separate ratings count
        "total_reviews": total,
        "average_rating": avg,
        "rating_breakdown": {},
        "rating_breakdown_percent": {},
        "positive_feedback_percent": None,
        "negative_feedback_percent": None,
        "aspect_ratings": {},
        "customers_say": None,
        "review_url": product_url,  # Alibaba shows product reviews on the product page
        "pages_scraped": pr.get("pages_scraped", 1) if pr else 0,
        "stop_reason": stop,
        "sort_used": "default",  # Alibaba exposes no review-order control
        "login_required": False,
        "review_source": pr.get("_source"),
        "review_tab": pr.get("tab_label"),
        # Completeness of product reviews (as Amazon); store feedback carries its own.
        **coverage,
        "collection_mode": "all",
        "store_reviews": store,
    }
    if detail.get("error"):
        summary["error"] = detail["error"]
    return summary