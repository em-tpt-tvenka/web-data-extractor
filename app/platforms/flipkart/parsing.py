from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
REL_RE = re.compile(r"\b(?:(\d+)|an?)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.I)

SPEC_HEADERS = {
    "general", "dimensions", "warranty", "connectivity", "display", "camera",
    "battery", "processor", "multimedia", "sound", "other",
    "network & connectivity", "memory & storage", "os & processor",
    "performance", "design", "platform", "storage", "ports", "software",
    "sensors", "audio", "video", "product details", "key features",
    "additional features", "composition", "usage details", "safety information",
}
SPEC_NOISE = {
    "specifications", "read more", "read less", "view more", "view less",
    "show more", "show less", "all details", "know more",
}

MFG_LABELS = [
    ("manufactured_by", r"manufactured\s+by|manufacturer(?:\s+(?:name|details|info|address))?|mfd\.?\s+by|mfg\.?\s+by"),
    ("marketed_by", r"marketed\s+by|marketer(?:\s+(?:name|details))?"),
    ("packed_by", r"packed\s+by|packer(?:\s+(?:name|details))?"),
    ("imported_by", r"imported\s+by|importer(?:\s+(?:name|details))?"),
    ("country_of_origin", r"country\s+of\s+origin"),
    ("generic_name", r"generic\s+name"),
    ("net_quantity", r"net\s+quantity"),
    ("manufacturing_date", r"(?:date\s+of\s+)?manufactur(?:e|ing)\s+date|month\s+and\s+year\s+of\s+manufacture"),
    ("customer_care", r"customer\s+care(?:\s+details)?"),
]
MFG_SPEC_KEYS = {
    "manufactured_by": ["Manufacturer", "Manufactured By", "Manufacturer Name", "Manufacturer Details"],
    "marketed_by": ["Marketed By", "Marketer"],
    "packed_by": ["Packed By", "Packer", "Packer Details"],
    "imported_by": ["Imported By", "Importer", "Importer Details"],
    "country_of_origin": ["Country of Origin"],
    "generic_name": ["Generic Name"],
    "net_quantity": ["Net Quantity"],
    "manufacturing_date": ["Manufacturing Date", "Date of Manufacture"],
    "customer_care": ["Customer Care", "Customer Care Details"],
}


def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def slug_upper(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (s or "").upper()).strip("_")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_number(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.]", "", str(s))
    if not s:
        return None
    return float(s) if "." in s else int(s)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def get_ids(url: str) -> dict:
    qs = parse_qs(urlparse(url).query)
    m = re.search(r"/p/(itm[0-9a-z]+)", url, re.I)
    return {
        "product_id": (qs.get("pid") or [None])[0],
        "item_id": m.group(1) if m else None,
        "listing_id": (qs.get("lid") or [None])[0],
    }


def section(text: str, start: str, ends=()) -> str:
    m = re.search(start, text, re.I | re.M)
    if not m:
        return ""
    rest = text[m.end():]
    cut = len(rest)
    for e in ends:
        n = re.search(e, rest, re.I | re.M)
        if n:
            cut = min(cut, n.start())
    return rest[:cut].strip()


def group_offers(block: str, start_regex: str) -> list:
    skip = {"best value for you", "apply", "know more", "t&c", ""}
    offers, cur = [], []
    for line in (l.strip() for l in block.splitlines()):
        if line.lower() in skip:
            continue
        if re.search(start_regex, line, re.I) and cur:
            offers.append(" | ".join(cur))
            cur = []
        cur.append(line)
    if cur:
        offers.append(" | ".join(cur))
    return offers


def weighted_average(breakdown: dict):
    total = sum(breakdown.values())
    if not total:
        return None
    return round(sum(int(k[0]) * v for k, v in breakdown.items()) / total, 2)


def sub_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) - n
    y, m = divmod(idx, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def parse_posted(text: str):
    if not text:
        return None, None
    today = date.today()
    m = REL_RE.search(text)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2).lower()
        if unit in ("second", "minute", "hour"):
            d = today
        elif unit == "day":
            d = today - timedelta(days=n)
        elif unit == "week":
            d = today - timedelta(weeks=n)
        elif unit == "month":
            d = sub_months(today, n)
        else:
            d = sub_months(today, 12 * n)
        return clean(m.group(0)), d.isoformat()
    if re.search(r"\byesterday\b", text, re.I):
        return "yesterday", (today - timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b|just now", text, re.I):
        return "today", today.isoformat()
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})[,\s]+(\d{4})", text)
    if m and m.group(2)[:3].lower() in MONTHS:
        try:
            return clean(m.group(0)), date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1))).isoformat()
        except ValueError:
            pass
    m = re.search(r"([A-Za-z]{3,9})[,\s]+(\d{4})", text)
    if m and m.group(1)[:3].lower() in MONTHS:
        return clean(m.group(0)), date(int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1).isoformat()
    return None, None


def parse_posted_date(text: str):
    _, iso = parse_posted(text)
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


SPEC_STOP_PATTERNS = [
    re.compile(r"^similar products\b", re.I),
    re.compile(r"^trending\b", re.I),
    re.compile(r"^more about\b", re.I),
    re.compile(r"^ratings?\s*(?:&|and)\s*reviews\b", re.I),
    re.compile(r"^customer ratings\b", re.I),
    re.compile(r"^questions?\s*(?:&|and)\s*answers\b", re.I),
    re.compile(r"^safe and secure\b", re.I),
    re.compile(r"^about us\b", re.I),
    re.compile(r"^contact us\b", re.I),
    re.compile(r"^flipkart internet\b", re.I),
    re.compile(r"^terms of use\b", re.I),
    re.compile(r"^privacy\b", re.I),
    re.compile(r"^hang on, loading content\b", re.I),
    re.compile(r"^consumer policy\b", re.I),
    re.compile(r"^group companies\b", re.I),
    re.compile(r"^help center\b", re.I),
    re.compile(r"^become a seller\b", re.I),
    re.compile(r"^©\s*\d{4}", re.I),
    re.compile(r"^ad\b", re.I),
]

SPEC_GARBAGE_KEYS = {
    "similar products", "trending", "ad", "hang on, loading content",
    "contact us", "about us", "careers", "flipkart stories", "help",
    "payments", "shipping", "faq", "security", "privacy", "sitemap",
    "karnataka, india", "registered office address:", "become a seller",
    "gift cards", "telephone", "mail us:", "cin : u51109ka2012ptc066107",
    "group companies", "consumer policy", "epr compliance", "myntra", "shopsy", "cleartrip"
}

SPEC_ROW_KEYS_NOT_HEADERS = {
    "in the box", "primary camera features", "secondary camera features",
    "other display features", "other details", "smart phone", "smartphone"
}

def is_spec_header(line: str) -> bool:
    l = line.strip().lower()
    if l in SPEC_ROW_KEYS_NOT_HEADERS:
        return False
    if l in SPEC_HEADERS:
        return True
    return bool(re.fullmatch(r"(?:general|display|camera|processor|os & processor|memory & storage|connectivity|call|multimedia|battery & power|audio|video|dimensions|warranty|additional)\s+(?:features|details|specifications)", l))


def _is_valid_spec_key(k: str) -> bool:
    if not k or len(k) <= 1 or len(k) > 80:
        return False
    if k.isdigit() or k in {"₹", "•", "a", "t"} or k.lower() in SPEC_GARBAGE_KEYS:
        return False
    return True


def parse_spec_text(block: str) -> dict:
    lines = [clean(l) for l in block.splitlines()]
    lines = [l for l in lines if l and l.lower() not in SPEC_NOISE]

    sections, current, buf = [], "General", []
    for l in lines:
        if any(p.search(l) for p in SPEC_STOP_PATTERNS):
            break
        if is_spec_header(l):
            sections.append((current, buf))
            current, buf = l.title(), []
        else:
            buf.append(l)
    sections.append((current, buf))

    specs = {}
    for name, items in sections:
        rows = {}
        i = 0
        while i < len(items):
            line = items[i]
            if ":" in line and not line.endswith(":"):
                k, _, v = line.partition(":")
                k_clean = k.strip()
                if _is_valid_spec_key(k_clean):
                    rows[k_clean] = v.strip()
                i += 1
            elif i + 1 < len(items):
                k_clean = line
                if _is_valid_spec_key(k_clean):
                    rows[k_clean] = items[i + 1]
                i += 2
            else:
                k_clean = line
                if _is_valid_spec_key(k_clean):
                    rows[k_clean] = ""
                i += 1
        if rows:
            specs.setdefault(name, {}).update(rows)
    return specs


def flatten_specs(specs: dict) -> dict:
    return {f"{sec} | {k}": v for sec, rows in specs.items() for k, v in rows.items()}


def find_spec(flat: dict, *names):
    for want in names:
        for k, v in flat.items():
            key = k.split(" | ", 1)[-1].strip().lower()
            if key == want.lower() and v:
                return v
    return None


def parse_breakdown(text: str) -> dict:
    found = re.findall(r"^([1-5])\s*★\s*(?:\n\s*)?([\d,]+)\s*$", text, re.M)
    return {f"{s}_star": to_number(c) for s, c in found}


def parse_feedback_percent(head: str):
    pos = re.search(r"(\d{1,3})\s*%\s*positive\s+feedback", head, re.I | re.S)
    neg = re.search(r"(\d{1,3})\s*%\s*negative\s+feedback", head, re.I | re.S)
    return (int(pos.group(1)) if pos else None, int(neg.group(1)) if neg else None)


def parse_review_summary(text: str) -> dict:
    head = text.split("User reviews sorted by")[0]
    m = re.search(r"([\d,]+)\s+ratings?\s+and\s+([\d,]+)\s+reviews?", head, re.I | re.S)
    breakdown = parse_breakdown(head)
    pos, neg = parse_feedback_percent(head)
    return {
        "total_ratings": to_number(m.group(1)) if m else (sum(breakdown.values()) or None),
        "total_reviews": to_number(m.group(2)) if m else None,
        "average_rating": weighted_average(breakdown),
        "rating_breakdown": breakdown,
        "positive_feedback_percent": pos,
        "negative_feedback_percent": neg,
    }


def split_reviewer(lines):
    if not lines:
        return "", "", 0
    last = lines[-1]
    if last.startswith(",") and len(lines) >= 2:
        return clean(lines[-2]), clean(last.lstrip(", ")), 2
    if len(lines) >= 2 and lines[-2].rstrip().endswith(",") and "," not in last:
        return clean(lines[-2].rstrip(", ")), clean(last), 2
    if "," in last:
        n, _, loc = last.partition(",")
        return clean(n), clean(loc), 1
    return clean(last), "", 1


def parse_reviews(text: str) -> list:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    starts, i = [], 0
    while i < len(lines):
        m = re.match(r"^([1-5](?:\.\d)?)\s*[•·]\s*(.*)$", lines[i])
        if m:
            starts.append((i, float(m.group(1)), m.group(2).strip(), 1))
            i += 1
            continue
        if re.fullmatch(r"[1-5](?:\.\d)?", lines[i]) and i + 1 < len(lines) and lines[i + 1] in ("•", "·"):
            title = lines[i + 2] if i + 2 < len(lines) else ""
            if title.lower().startswith("review for"):
                title, used = "", 2
            else:
                used = 3
            starts.append((i, float(lines[i]), title, used))
            i += used
            continue
        i += 1

    reviews = []
    for n, (idx, rating, title, used) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = lines[idx + used:end]

        v_idx = next((k for k, l in enumerate(block)
                      if re.search(r"verified purchase|certified buyer", l, re.I)), None)
        meta_text = ""
        if v_idx is not None:
            meta_text = " ".join(block[v_idx:v_idx + 3])
            cut = v_idx + 1
            for k in (1, 2):
                if v_idx + k < len(block) and (
                        REL_RE.search(block[v_idx + k]) or block[v_idx + k] in ("·", "•")
                        or re.search(r"\b(today|yesterday)\b|[A-Za-z]{3,9}[,\s]+\d{4}", block[v_idx + k], re.I)):
                    cut = v_idx + k + 1
            block = block[:cut]
        else:
            meta_text = " ".join(block[-4:])
        posted_rel, posted_date = parse_posted(meta_text)

        variant_raw, variant, var_idx = None, {}, -1
        for k, l in enumerate(block):
            if l.lower().startswith("review for"):
                variant_raw, var_idx = clean(l.split(":", 1)[-1]), k
                for part in re.split(r"\s*[•·]\s*", variant_raw):
                    kk, _, vv = part.strip().partition(" ")
                    if kk and vv:
                        variant[kk] = vv.strip()
                break

        h_idx = next((k for k, l in enumerate(block) if re.match(r"^Helpful", l, re.I)), None)
        helpful = second = None
        if h_idx is not None:
            hm = re.search(r"(\d[\d,]*)", block[h_idx])
            nums_after = [to_number(l) for l in block[h_idx + 1:h_idx + 4] if re.fullmatch(r"[\d,]+", l)]
            if hm:
                helpful = to_number(hm.group(1))
                second = nums_after[0] if nums_after else None
            else:
                helpful = nums_after[0] if nums_after else None
                second = nums_after[1] if len(nums_after) > 1 else None

        pre_end = h_idx if h_idx is not None else (v_idx if v_idx is not None else len(block))
        pre = block[var_idx + 1:pre_end]
        name, location, used_lines = split_reviewer(pre)
        body_lines = pre[:len(pre) - used_lines] if used_lines else pre
        body = clean(" ".join(l for l in body_lines if l.lower() not in ("more", "read more", "...")))

        reviews.append({
            "reviewer_name": name,
            "rating": rating,
            "title": title,
            "review_text": body,
            "verified_purchase": bool(re.search(r"verified purchase|certified buyer", meta_text, re.I)),
            "posted_date": posted_date,
            "posted_relative": posted_rel,
            "helpful_count": helpful,
        })
    return reviews


def parse_aspect_head(head: str, chips):
    chip_labels = {clean(c.split("\n")[0]).lower() for c in chips}
    rating = None
    for l in (clean(x) for x in head.splitlines()):
        if l.lower() in chip_labels:
            continue
        if re.fullmatch(r"[1-5](?:\.\d)?", l):
            rating = float(l)
            break
    pos, neg = parse_feedback_percent(head)
    return rating, pos, neg


def build_review_url(page_url: str) -> str:
    u = urlparse(page_url)
    qs = parse_qs(u.query)
    path = u.path.replace("/p/", "/product-reviews/")
    q = {k: qs[k][0] for k in ("pid", "lid") if k in qs}
    q["marketplace"] = qs.get("marketplace", ["FLIPKART"])[0]
    return f"{u.scheme}://{u.netloc}{path}?{urlencode(q)}"


# Each Flipkart review listing (one sort order) stops after 10 pages of 10
# reviews (verified live: page 11+ is empty even when thousands are reported);
# the four sort orders are separate listings, each with its own 100-review
# window. Star, aspect (aid) and certifiedBuyer URL filters did not partition
# the reviews (ignored / identical / empty when loaded by URL).
LISTING_CAP = 100
REVIEW_SORT_ORDERS = {
    "helpful": "MOST_HELPFUL",
    "recent": "MOST_RECENT",
    "positive": "POSITIVE_FIRST",
    "negative": "NEGATIVE_FIRST",
}


def review_listing_url(review_url: str, sort: str) -> str:
    """Review listing for one sort order (``review_url`` from build_review_url)."""
    u = urlparse(review_url)
    qs = {k: v[0] for k, v in parse_qs(u.query).items() if k not in ("page", "sortOrder", "aid", "certifiedBuyer")}
    qs.update({"aid": "overall", "certifiedBuyer": "false", "sortOrder": REVIEW_SORT_ORDERS[sort]})
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(qs)}"


def is_login_redirect(url: str | None) -> bool:
    """Flipkart intermittently redirects review pages to /login; a retry loads them."""
    return "/login" in urlparse(url or "").path


def review_key(r: dict) -> tuple:
    """Identity of a parsed review across pages and sort orders."""
    return (r.get("reviewer_name"), r.get("rating"), r.get("title"), (r.get("review_text") or "")[:80],
            r.get("posted_relative"), r.get("helpful_count"))


def review_coverage(available, extracted, collect_all: bool, listings: list[dict], blocked: bool = False) -> dict:
    """Reported review count vs. reviews extracted (same keys as Amazon /
    Alibaba). ``complete`` is None when Flipkart shows no review count."""
    out = {"reviews_available": available, "reviews_extracted": extracted,
           "coverage_percent": round(min(extracted, available) / available * 100, 1)
           if available and extracted is not None else None,
           "complete": None, "incomplete_reason": None}
    if available is None or extracted is None:
        return out
    out["complete"] = extracted >= available
    if out["complete"]:
        return out
    failed = [l for l in listings if l.get("failed")]
    if blocked:
        reason = "Flipkart blocked the review pages (captcha / access denied)"
    elif not collect_all:
        reason = "collect_all_reviews is off; only review_pages_per_product pages were loaded"
    elif failed:
        reason = (f"{len(failed)} review listing(s) could not be loaded (Flipkart kept redirecting "
                  "to login); see review_summary.listings")
    else:
        reason = (f"Flipkart lists at most {LISTING_CAP} reviews per sort order; all "
                  f"{len(listings)} sort orders were loaded to their last page")
    out["incomplete_reason"] = reason
    return out


def with_page(url: str, p: int) -> str:
    u = urlparse(url)
    qs = {k: v[0] for k, v in parse_qs(u.query).items()}
    qs["page"] = str(p)
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(qs)}"


def price_from_text(text: str) -> dict:
    out = {"selling": None, "mrp": None, "discount": None}
    head = text[:8000]
    m = re.search(r"(?:↓\s*(\d{1,2})\s*%)|(?:\b(\d{1,2})\s*%\s*off\b)", head, re.I)
    if not m:
        return out
    out["discount"] = f"{m.group(1) or m.group(2)}%"
    window = head[max(0, m.start() - 160): m.end() + 160]
    window = re.sub(r"(?:Extra\s*)?₹\s*[\d,]+\s*off", " ", window, flags=re.I)
    nums = [to_number(x) for x in re.findall(r"₹\s*([\d,]{3,})", window)]
    nums = sorted({n for n in nums if n})
    if len(nums) >= 2:
        mrp = nums[-1]
        # Ignore add-ons/EMI fees that are tiny fractions (< 25%) of the MRP when a higher price exists
        valid_selling = [n for n in nums[:-1] if n >= mrp * 0.25]
        selling = valid_selling[0] if valid_selling else nums[0]
        out["mrp"], out["selling"] = mrp, selling
    return out


def _is_invalid_mfg_val(val: str | None) -> bool:
    if not val:
        return True
    v = str(val).strip().lower()
    if len(v) < 3 or v in SPEC_NOISE:
        return True
    if any(s in v for s in ("show more", "read more", "view all", "view more", "all details",
                            "know more", "details", "manufacturer info", "description",
                            "features", "dimensions", "display", "camera", "battery", "general")):
        return True
    return False


def extract_manufacturer(text: str, flat: dict, brand: str | None = None) -> dict | None:
    out = {}
    for k, names in MFG_SPEC_KEYS.items():
        v = find_spec(flat, *names)
        if v and not _is_invalid_mfg_val(v):
            out[k] = clean(v)

    if not out:
        lines = [clean(l) for l in text.splitlines() if l.strip()]
        label_only = re.compile(r"^(?:" + "|".join(rx for _, rx in MFG_LABELS) + r")\s*:?\s*$", re.I)
        for i, l in enumerate(lines):
            for key, rx in MFG_LABELS:
                if key in out:
                    continue
                m = re.fullmatch(r"(?:" + rx + r")\s*[:\-]\s*(.+)", l, re.I)
                if m:
                    val = m.group(1).strip()
                    if not _is_invalid_mfg_val(val):
                        out[key] = val
                        break
                if re.fullmatch(r"(?:" + rx + r")\s*:?", l, re.I) and i + 1 < len(lines):
                    val = lines[i + 1]
                    if not label_only.match(val) and not _is_invalid_mfg_val(val):
                        out[key] = val
                        break

    # Clean any invalid values that might have slipped through
    for k in list(out.keys()):
        if _is_invalid_mfg_val(out[k]):
            out.pop(k, None)

    if not out:
        if brand:
            return {"name": brand, "manufactured_by": brand}
        return None

    name = out.get("manufactured_by") or out.get("marketed_by")
    if _is_invalid_mfg_val(name):
        name = None
    if not name:
        name = brand
    if name:
        out["name"] = name.split(",")[0].strip()
    if _is_invalid_mfg_val(out.get("manufactured_by")):
        if brand:
            out["manufactured_by"] = brand
        else:
            out.pop("manufactured_by", None)
    return out


def parse_capacity(s):
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*([A-Za-z]+)", str(s))
    if not m:
        return None
    unit = m.group(2).lower()
    unit = {
        "l": "litre", "ltr": "litre", "litres": "litre", "liter": "litre", "liters": "litre",
        "litre": "litre", "gb": "GB", "tb": "TB", "mb": "MB",
        "ml": "ml", "g": "g", "gm": "g", "grams": "g", "gram": "g",
        "kg": "kg", "kgs": "kg", "oz": "oz",
    }.get(unit, unit)
    return {"value": to_number(m.group(1)), "unit": unit}


def parse_energy_rating(s):
    if not s:
        return None
    m = re.search(r"([1-5])\s*[-]?\s*star", str(s), re.I)
    if m:
        return f"{m.group(1)}_star"
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or None


def parse_warranty(data: dict):
    flat = data.get("specifications_flat", {})
    candidates = [find_spec(flat, "Warranty Summary", "Warranty", "Warranty Period", "Domestic Warranty")]
    candidates += data.get("warranty_and_policies", {}).get("warranty", [])
    for text in filter(None, candidates):
        m = re.search(r"(\d+)\s*(year|yr|month)", text, re.I)
        if m:
            n = int(m.group(1))
            months = n * 12 if m.group(2).lower().startswith("y") else n
            wtype = "manufacturer" if re.search(r"manufacturer|brand", text, re.I) else None
            return months, wtype
    return None, None


def availability_status(raw):
    r = (raw or "").lower()
    if "instock" in r:
        return "in_stock"
    if "outofstock" in r or "soldout" in r:
        return "out_of_stock"
    if "preorder" in r:
        return "pre_order"
    return None


def dedupe_images(urls):
    seen, out = set(), []
    for u in urls:
        if not u:
            continue
        u = re.sub(r"/image/\d+/\d+/", "/image/832/832/", u.split("?")[0])
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# --------------------------------------------------------------------------
# Seller / retailer parsing (additive)
# --------------------------------------------------------------------------
SELLER_TAB_LABELS = {"OFFERS", "DELIVERY", "NO COST EMI", "EMI", "COD", "REPLACE"}
SELLER_TAB_TITLES = {
    "OFFERS": "Offers", "DELIVERY": "Delivery", "NO COST EMI": "No Cost EMI",
    "EMI": "EMI", "COD": "COD", "REPLACE": "Replace",
}
_OFFER_HEAD_RE = re.compile(
    r"^(Bank Offer|Partner Offer|Special Price|SpecialPrice|No Cost EMI|Combo Offer|"
    r"Exchange Offer|Wallet Offer|Cashback|Coupon)$", re.I)
_RATING_ONLY_RE = re.compile(r"^(\d(?:\.\d)?)\s*★?$")


def _lines(text: str) -> list:
    return [clean(l) for l in (text or "").replace("\t", "\n").splitlines() if clean(l)]


def parse_indian_count(s):
    """'33.1L+' -> 3310000, '2.5K' -> 2500, '1Cr' -> 10000000."""
    if not s:
        return None
    m = re.match(r"\s*([\d.,]+)\s*(Cr|L|K|M)?", str(s), re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"cr": 10_000_000, "l": 100_000, "m": 1_000_000, "k": 1_000}.get((m.group(2) or "").lower(), 1)
    return int(round(n * mult))


def parse_seller_profile(text: str) -> dict:
    """Seller profile panel: name, rating, tenure, sold / quality / speed, about, overall ratings."""
    lines = _lines(text)
    out = {
        "name": None, "rating": None, "tenure": None, "tenure_years": None,
        "products_sold": None, "products_sold_estimate": None,
        "quality_score_percent": None, "speed_score_percent": None,
        "about": [], "overall_ratings": {},
    }
    if not lines:
        return out
    skip = {"←", "<", "back", "close", "✕"}
    out["name"] = next((l for l in lines if l.lower() not in skip), None)

    m = re.search(r"(\d(?:\.\d)?)\s*★", text)
    if not m and len(lines) > 1:  # star drawn as an icon, not text: "4.5 • 3 years with Flipkart"
        m = re.search(r"\b(\d\.\d)\b", lines[1])
    out["rating"] = float(m.group(1)) if m else None
    m = re.search(r"(\d+)\s*(years?|months?)\s*with\s*Flipkart", text, re.I)
    if m:
        out["tenure"] = f"{m.group(1)} {m.group(2).lower()}"
        out["tenure_years"] = int(m.group(1)) if m.group(2).lower().startswith("year") else round(int(m.group(1)) / 12, 2)

    m = re.search(r"([\d.,]+\s?(?:Cr|L|K|M)?\+?)\s*\n?\s*Products?\s+Sold", text, re.I)
    if m:
        out["products_sold"] = clean(m.group(1))
        out["products_sold_estimate"] = parse_indian_count(m.group(1))
    m = re.search(r"(\d{1,3})\s*%\s*\n?\s*Quality Score", text, re.I)
    out["quality_score_percent"] = int(m.group(1)) if m else None
    m = re.search(r"(\d{1,3})\s*%\s*\n?\s*Speed Score", text, re.I)
    out["speed_score_percent"] = int(m.group(1)) if m else None

    low = [l.lower() for l in lines]
    if "about seller" in low:
        i = low.index("about seller") + 1
        while i < len(lines) and low[i] not in ("overall ratings", "see other sellers"):
            out["about"].append(lines[i])
            i += 1
    if "overall ratings" in low:
        i = low.index("overall ratings") + 1
        while i < len(lines) and low[i] != "see other sellers":
            if i + 1 < len(lines):
                r = _RATING_ONLY_RE.match(lines[i + 1])
                if r and not _RATING_ONLY_RE.match(lines[i]):
                    out["overall_ratings"][lines[i]] = float(r.group(1))
                    i += 2
                    continue
            i += 1
    return out


def parse_seller_card(text: str, name) -> dict:
    """The seller's card inside the 'All Sellers' panel (price, EMI, delivery, highlights)."""
    lines = _lines(text)
    if not lines:
        return {}
    start = None
    if name:
        for i, l in enumerate(lines):
            if l.lower().startswith(name.lower()):
                start = i
                break
    if start is None:
        return {}
    block = [lines[start][len(name):].strip()] if len(lines[start]) > len(name) else []
    for l in lines[start + 1:]:
        if l.upper() in SELLER_TAB_LABELS or l.upper() == "MORE" or re.fullmatch(r"\+\d+\s*MORE", l, re.I):
            break
        block.append(l)
    block = [b for b in block if b]
    joined = " | ".join(block)
    m_rating = re.search(r"(\d\.\d)\s*★", joined)
    if not m_rating:  # star is an icon: rating sits alone on its own line
        r = next((re.fullmatch(r"(\d\.\d)", x) for x in block if re.fullmatch(r"\d\.\d", x)), None)
        m_rating = r
    m_del = re.search(r"((?:Next Day|Same Day|Express)?\s*Delivery[^|]*)", joined, re.I)
    return {
        "rating": float(m_rating.group(1)) if m_rating else None,
        "delivery_note": clean(m_del.group(1)) if m_del else None,
        "highlights": [b for b in block if re.search(r"Country of Origin|Bank Offer|Warranty", b, re.I)],
    }


def parse_offers_tab(body: list) -> dict:
    """Group offer lines by their heading (Bank Offer / Partner Offer / Special Price / ...)."""
    groups: dict = {}
    cur = "Other"
    item: list = []
    for raw in body:
        if raw.upper() == "T&C":
            continue
        ends = raw.upper().endswith("T&C")
        line = re.sub(r"\s*T&C\s*$", "", raw).strip()
        if not item and _OFFER_HEAD_RE.match(line) and not ends:
            cur = re.sub(r"(?i)^specialprice$", "Special Price", line)
            cur = cur.title() if cur.islower() else cur
            groups.setdefault(cur, [])
            continue
        if line:
            item.append(line)
        if ends:
            groups.setdefault(cur, []).append(" ".join(item))
            item, cur = [], "Other"
    if item:
        groups.setdefault(cur, []).append(" ".join(item))
    return {k: v for k, v in groups.items() if v}


def parse_replace_tab(body: list) -> dict:
    low = [l.lower() for l in body]
    if "type accepted" not in low:
        return {"details": body}
    i = low.index("type accepted")
    cells = body[i + 1:i + 4] + [None] * 3
    rest = [t for t in (re.sub(r"^[•\u2022]\s*", "", l) for l in body[i + 4:]) if t]
    return {
        "validity": cells[0], "covers": cells[1], "type_accepted": cells[2],
        "terms": rest,
    }


def parse_seller_tab(tab: str, text: str, name) -> dict:
    """Parse one tab (OFFERS / DELIVERY / NO COST EMI / EMI / COD / REPLACE) of the seller panel."""
    lines = _lines(text)
    i = 0
    skip = {"←", "<", "back"}
    while i < len(lines) and (
        (name and lines[i].lower() == name.lower())
        or lines[i].upper() in SELLER_TAB_LABELS or lines[i].lower() in skip
    ):
        i += 1
    body = [l for l in lines[i:] if l.upper() not in ("ADD TO CART", "BUY NOW")]
    tab = tab.upper()
    if tab == "OFFERS":
        return parse_offers_tab(body)
    if tab == "REPLACE":
        return parse_replace_tab(body)
    return {"details": body}


def rating_near(text: str, name) -> float | None:
    """Rating printed right after the seller name, e.g. 'Seller: HonasaConsumerLtd 4.5★'."""
    if not text or not name:
        return None
    m = re.search(re.escape(name) + r"[^\d\n]{0,6}\n?\s*(\d\.\d)\b", text, re.I)
    return float(m.group(1)) if m else None