from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import quote, urlparse


def clean_text(text):
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", "", str(text))
    return " ".join(cleaned.split()) or None


def parse_price(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"(?:US\$|\$)?\s?([\d,]+\.?\d*)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def search_url(query: str, page_no: int) -> str:
    base = f"https://www.bestbuy.com/site/searchpage.jsp?st={quote(query)}"
    return base if page_no <= 1 else f"{base}&cp={page_no}"


def product_id_from_url(url: str) -> str | None:
    """Best Buy's SKU appears either as digits before ".p" (legacy URLs like
    .../123456.p) or as an alphanumeric token as the last /product/.../ segment
    (current URLs like .../product/.../JCQ6HRGR32); skuId= query params are a
    third, older form. Try each in turn before falling back to any 5+ digit run."""
    path = urlparse(url).path
    m = re.search(r"(\d{5,})\.p(?:/|$)", path)
    if m:
        return m.group(1)
    m = re.search(r"/product/[^?#]*/([A-Za-z0-9]{6,12})(?:/|$)", path, re.I)
    if m and any(c.isdigit() for c in m.group(1)):
        return m.group(1).upper()
    m = re.search(r"skuId=([A-Za-z0-9]{5,12})", url, re.I)
    if m:
        return m.group(1).upper()
    # Last-resort fallback: a bare run of 5+ digits, but only as a whole token
    # (word-boundary on both sides). Without the boundary this also matches
    # digits embedded in non-product category/campaign ids such as
    # "abcat0010000.c" or "pcmcat1774297472214.c" (nav/trade-in/deal pages),
    # which would otherwise be misidentified as product SKUs.
    m = re.search(r"\b(\d{5,})\b", path)
    return m.group(1) if m else None


def _safe_url(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get("url") or obj.get("contentUrl") or obj.get("thumbnailUrl")
    return None


# bbystatic.com's (Scene7-style) image server encodes the requested render
# size as ";maxHeight=N;maxWidth=N" path segments, before any "?" query
# string. Confirmed live on bestbuy.com search-result tiles: the SAME image
# (same UUID filename) appears several times on one page at different sizes,
# e.g. ".../<uuid>.jpg;maxHeight=100;maxWidth=150" (thumbnail) and
# ".../<uuid>.jpg;maxHeight=427;maxWidth=640?format=webp" (gallery size).
# Stripping only the "?" query (the previous approach) does not normalize
# these, so the same image was kept twice under different size variants.
_IMG_VARIANT_RE = re.compile(r";maxHeight=\d+;maxWidth=(\d+)", re.I)


def _img_key(url: str) -> str:
    """Identity key for an image URL with any bbystatic size segment and
    query string removed, so same-image/different-size duplicates collapse."""
    return _IMG_VARIANT_RE.sub("", url.split("?")[0])


def dedupe_images(urls: list) -> list:
    """De-dupe product images by identity (see _img_key), keeping whichever
    *observed* size variant is largest for each image rather than inventing
    an un-observed size (unlike Flipkart's image dedup, where rewriting to a
    fixed 832x832 path was a confirmed-valid convention; that has not been
    confirmed for bbystatic.com)."""
    best: dict = {}
    order: list = []
    for u in urls or []:
        if not u:
            continue
        key = _img_key(u)
        if key not in best:
            best[key] = u
            order.append(key)
            continue
        m_new = _IMG_VARIANT_RE.search(u)
        m_cur = _IMG_VARIANT_RE.search(best[key])
        new_w = int(m_new.group(1)) if m_new else 0
        cur_w = int(m_cur.group(1)) if m_cur else 0
        if new_w > cur_w:
            best[key] = u
    return [best[k] for k in order]


def parse_blocks(script_texts: list[str]) -> list:
    blocks = []
    for raw in script_texts:
        if not raw or not raw.strip():
            continue
        try:
            blocks.append(json.loads(raw))
        except Exception:
            continue
    return blocks


def product_from_blocks(blocks: list) -> dict:
    out = {
        "product_name": None, "description": None, "brand": None, "sku": None,
        "model_number": None, "price_amount": None, "original_price": None,
        "availability": None, "images": [], "videos": [],
        "average_rating": None, "total_reviews": None, "stock_quantity": None,
    }
    seen_vid = set()

    def node(obj):
        if isinstance(obj, list):
            for x in obj:
                node(x)
            return
        if not isinstance(obj, dict):
            return
        if obj.get("name") and not out["product_name"] and len(str(obj["name"])) > 3:
            out["product_name"] = clean_text(obj["name"])
        if obj.get("sku") and not out["sku"]:
            out["sku"] = str(obj["sku"])
        if obj.get("model") and not out["model_number"]:
            out["model_number"] = str(obj["model"])
        if obj.get("description") and not out["description"]:
            out["description"] = clean_text(obj["description"])
        brand = obj.get("brand")
        if not out["brand"]:
            if isinstance(brand, dict) and brand.get("name"):
                out["brand"] = clean_text(brand["name"])
            elif isinstance(brand, str):
                out["brand"] = clean_text(brand)
        offers = obj.get("offers")
        if offers:
            for off in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(off, dict):
                    continue
                price = parse_price(off.get("price") or off.get("lowPrice") or off.get("highPrice"))
                if price is not None and out["price_amount"] is None:
                    out["price_amount"] = price
                high = parse_price(off.get("highPrice"))
                if high is not None and out["original_price"] is None:
                    out["original_price"] = high
                avail = str(off.get("availability") or "").lower()
                if "instock" in avail:
                    out["availability"] = "in_stock"
                elif "outofstock" in avail:
                    out["availability"] = "out_of_stock"
                # schema.org Offer.inventoryLevel (a QuantitativeValue) is the
                # documented place an exact stock count would appear in
                # JSON-LD; Best Buy typically omits it (binary in/out of
                # stock only), so this is populated only when actually
                # present, never fabricated.
                if out["stock_quantity"] is None:
                    inv = off.get("inventoryLevel")
                    qty = inv.get("value") if isinstance(inv, dict) else inv
                    if qty is not None:
                        try:
                            out["stock_quantity"] = int(qty)
                        except (ValueError, TypeError):
                            pass
        agg = obj.get("aggregateRating")
        if isinstance(agg, dict):
            if out["average_rating"] is None and agg.get("ratingValue") is not None:
                try:
                    out["average_rating"] = float(agg["ratingValue"])
                except (ValueError, TypeError):
                    pass
            if out["total_reviews"] is None:
                cnt = agg.get("reviewCount") or agg.get("ratingCount")
                if cnt is not None:
                    try:
                        out["total_reviews"] = int(cnt)
                    except (ValueError, TypeError):
                        pass
        img = obj.get("image")
        if img:
            for im in (img if isinstance(img, list) else [img]):
                u = _safe_url(im)
                if u:
                    out["images"].append(u)
        vid = obj.get("video") or obj.get("videos")
        if vid:
            for item in (vid if isinstance(vid, list) else [vid]):
                if isinstance(item, dict):
                    u = item.get("contentUrl") or item.get("url") or item.get("embedUrl")
                    if u and u not in seen_vid:
                        seen_vid.add(u)
                        out["videos"].append(u)
        for k, v in obj.items():
            if k not in ("offers", "image", "video", "videos", "brand", "aggregateRating"):
                node(v)

    for b in blocks:
        node(b)
    out["images"] = dedupe_images(out["images"])
    return out


def review_key(r: dict):
    """Duplicate key for a review: an explicit identifier (JSON-LD's
    identifier/@id) when present, else a reviewer+rating+title+text+date
    composite - same approach as Amazon's review_key - used both to drop an
    exact duplicate within one source (e.g. the same JSON-LD review node
    appearing in two <script> blocks) and to merge Best Buy's two review
    sources (the initial JSON-LD batch and the full DOM render after
    exhausting "Show More Reviews")."""
    return r.get("review_id") or (r.get("reviewer_name"), r.get("rating"), r.get("title"),
                                  (r.get("review_text") or "")[:80], r.get("posted_date"))


def merge_reviews(*lists: list[dict]) -> list[dict]:
    """Best Buy's initial JSON-LD review batch and the full DOM render (after
    clicking "Show More Reviews" to exhaustion) overlap on the first batch;
    keep one copy per review (keyed by review_key), filling empty fields from
    whichever copy has them and preferring the longer text - the same merge
    Amazon uses for its page-vs-review-tab overlap."""
    merged: dict = {}
    order: list = []
    for lst in lists:
        for r in lst:
            key = review_key(r)
            if key not in merged:
                merged[key] = dict(r)
                order.append(key)
                continue
            cur = merged[key]
            for f, v in r.items():
                if v in (None, [], "", False):
                    continue
                if cur.get(f) in (None, [], "", False) or \
                        (f == "review_text" and len(v) > len(cur.get(f) or "")):
                    cur[f] = v
    return [merged[k] for k in order]


def reviews_from_blocks(blocks: list) -> list[dict]:
    reviews: list[dict] = []

    def node(obj):
        if isinstance(obj, list):
            for x in obj:
                node(x)
            return
        if not isinstance(obj, dict):
            return
        rlist = obj.get("review")
        if isinstance(rlist, list):
            for r in rlist:
                if not isinstance(r, dict):
                    continue
                rr = r.get("reviewRating")
                val = rr.get("ratingValue") if isinstance(rr, dict) else rr
                author = r.get("author")
                name = author.get("name") if isinstance(author, dict) else (str(author) if author else None)
                reviews.append({
                    "review_id": r.get("identifier") or r.get("@id"),
                    "rating": _num(val),
                    "title": clean_text(r.get("name") or r.get("headline")),
                    "review_text": clean_text(r.get("reviewBody") or r.get("description") or r.get("comment")),
                    "reviewer_name": clean_text(name),
                    "posted_date": _norm_date(r.get("datePublished")),
                    "verified_purchase": False,
                    "helpful_count": None,
                })
        for v in obj.values():
            node(v)

    for b in blocks:
        node(b)
    # The same review node can appear in more than one JSON-LD <script> block
    # on one page; drop exact duplicates by review_key before returning.
    seen: set = set()
    deduped: list[dict] = []
    for r in reviews:
        k = review_key(r)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return deduped


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return None


def _norm_date(raw) -> str | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw).split("T")[0]).isoformat()
    except ValueError:
        for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(str(raw).strip(), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _spec_get(specs: dict, *names: str):
    lowered = {str(k).strip().lower(): v for k, v in (specs or {}).items()}
    for want in names:
        v = lowered.get(want.lower())
        if v not in (None, "", [], {}):
            return v
    return None


# Best Buy's spec table (already captured generically into `specifications`
# by _JS_SPECS) is confirmed, by the prior audit, to carry manufacturer
# details on at least some product types; the exact row label could not be
# re-confirmed in this pass because product-page navigation was blocked by
# the US proxy throughout (see the accompanying report), so this searches
# several plausible row labels rather than assuming one fixed name - the
# same defensive, no-fabrication pattern already used for this field on
# Walmart/Amazon in this codebase.
def extract_manufacturer(specs: dict, brand: str | None = None) -> dict | None:
    name = _spec_get(specs, "Manufacturer", "Manufacturer Name", "Manufactured By", "Brand Name")
    country = _spec_get(specs, "Country of Origin", "Country Of Origin", "Made In")
    if not name and not country:
        return None
    out: dict = {}
    if name:
        out["name"] = str(name).strip()
    elif brand:
        out["name"] = brand
    if country:
        out["country_of_origin"] = str(country).strip()
    return out or None


_WARRANTY_DURATION_RE = re.compile(r"(\d+)\s*[-\s]?\s*(year|yr|month)s?", re.I)


def extract_warranty(specs: dict, page_text: str | None = None) -> dict:
    """Manufacturer warranty duration/type: the spec table first (same place
    this is read from on other platforms in this codebase), else a
    "<N> year warranty"-style phrase in the page's own text. Best Buy's own
    "Geek Squad Protection Plan" is a separate, priced add-on, not a
    manufacturer warranty, and is deliberately excluded here (same reasoning
    Walmart's warranty_info uses for Walmart Protection Plan) to avoid
    fabricating a warranty_type for something not actually the
    manufacturer's own."""
    out: dict = {}
    raw = _spec_get(specs, "Warranty", "Warranty Information", "Manufacturer Warranty", "Warranty Type")
    if not raw and page_text:
        m = re.search(r"([^.\n]{0,80}\bwarranty\b[^.\n]{0,80})", page_text, re.I)
        if m and "geek squad" not in m.group(1).lower() and "protection plan" not in m.group(1).lower():
            raw = m.group(1)
    if raw:
        raw = str(raw).strip()
        dm = _WARRANTY_DURATION_RE.search(raw)
        if dm:
            n = int(dm.group(1))
            out["duration_months"] = n * 12 if dm.group(2).lower().startswith("y") else n
        if re.search(r"manufactur", raw, re.I):
            out["warranty_type"] = "manufacturer"
        out["warranty_text"] = raw
    return out


def extract_delivery(page_text: str | None) -> dict:
    """Shipping/delivery text visible near the buy box (e.g. "Free shipping"
    / "Get it by ..."), read from the page's own text rather than a fixed CSS
    selector (not independently re-confirmed live this pass; see report)."""
    out: dict = {}
    if not page_text:
        return out
    m = re.search(r"\b(free (?:1-day |2-day |same-day |next-day )?shipping|free delivery)\b", page_text, re.I)
    if m:
        out["delivery_text"] = m.group(1).strip()
        out["free_delivery"] = True
    date_m = re.search(r"\b(?:get it by|arrives by)\s+([A-Za-z]{3,9}\.?\s+\d{1,2}(?:-\d{1,2})?)", page_text, re.I)
    if date_m:
        out.setdefault("delivery_text", date_m.group(0).strip())
        out["estimated_delivery_date"] = date_m.group(1).strip()
    return out


def extract_trade_in(page_text: str | None) -> dict:
    """Best Buy's own trade-in program, when advertised on the product page
    itself (the program is confirmed to exist as a site feature, at
    /site/best-buy-trade-in/trade-in-offers/...; only an actual per-product
    on-page mention is captured here, never a fabricated estimate)."""
    out: dict = {}
    if not page_text:
        return out
    m = re.search(r"([^.\n]{0,40}\btrade[- ]?in\b[^.\n]{0,80})", page_text, re.I)
    if m:
        out["trade_in_text"] = m.group(1).strip()
        val = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", m.group(1))
        if val:
            try:
                out["trade_in_value_estimate"] = float(val.group(1).replace(",", ""))
            except ValueError:
                pass
    return out
