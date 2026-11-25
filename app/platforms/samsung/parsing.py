from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.platforms import common

# Colour vocabulary used to recover a colour from a product title or URL slug
# when the specification table does not expose one. Base colours only; marketing
# compound names (e.g. "Mystic White") resolve to their base colour here, while
# an explicit specification value is preferred verbatim in the mapper.
_COLOR_WORDS = {
    "black", "white", "silver", "gold", "blue", "green", "red", "pink", "purple",
    "grey", "gray", "graphite", "titanium", "cream", "beige", "navy", "lavender",
    "mint", "bronze", "copper", "yellow", "orange", "brown", "violet", "burgundy",
    "charcoal", "sand", "ivory", "rose", "teal", "platinum", "amber", "coral",
    "khaki", "onyx", "sapphire",
}

# Values that must never be accepted as a model number regardless of their source.
_BAD_MODEL_VALUES = {
    "purchase price", "price", "mrp", "available", "unavailable", "in stock",
    "out of stock", "yes", "no", "true", "false", "n/a", "na", "none", "model",
}

NON_SAMSUNG_BRANDS = [
    "apple", "macbook", "mac", "iphone", "ipad", "airpods", "imac", "apple watch",
    "hp", "hewlett packard", "pavilion", "spectre", "envy", "omen", "victus",
    "dell", "alienware", "inspiron", "xps", "vostro", "latitude",
    "lenovo", "thinkpad", "ideapad", "legion", "yoga", "loq",
    "acer", "asus", "rog", "tuf", "zenbook", "vivobook", "toshiba", "msi",
    "microsoft", "surface", "sony", "vaio", "bravia", "razer", "lg", "gram",
    "huawei", "xiaomi", "redmi", "poco", "oneplus", "oppo", "vivo", "realme",
    "motorola", "moto", "pixel", "google pixel", "nokia", "honor",
]

EXCLUDE_URL_PATTERNS = [
    "/buy/", "/compare/", "buying-guide", "/support/", "/accessories/",
    "accessory", "/campaign/", "help-me-choose", "all-smartphones", "all-computers",
    "all-tvs", "all-refrigerators", "all-audio-devices", "all-tablets",
    "all-watches", "all-monitors", "all-washers-and-dryers", "all-air-conditioners",
    "/b2b/", "/business/", "terms", "faq", "overview",
]

_PRODUCT_SEGMENTS = (
    "/smartphones/", "/computers/", "/tablets/", "/tvs/", "/watches/",
    "/refrigerators/", "/monitors/", "/audio-devices/", "/washers-and-dryers/",
    "/air-conditioners/",
)

PRODUCT_MODEL_SUFFIX_REGEX = re.compile(
    r"-([a-z]{1,4}\d{1,4}[a-z0-9\-]+|[a-z0-9]{3,}-[a-z0-9]{3,})/?$", re.I)

_CATEGORY_KEYWORDS = {
    "laptops": ("laptop", "laptops", "galaxy book", "galaxy-book", "notebook", "copilot"),
    "smartphones": ("smartphone", "smartphones", "phone", "phones", "mobile", "galaxy s", "galaxy a", "fold", "flip"),
    "tablets": ("tablet", "tablets", "galaxy tab", "tab"),
    "tvs": ("tv", "tvs", "television", "neo qled", "qled", "oled", "crystal uhd"),
    "watches": ("watch", "watches", "galaxy watch", "ring", "galaxy ring"),
    "refrigerators": ("fridge", "fridges", "refrigerator", "refrigerators"),
    "washers-and-dryers": ("washer", "washers", "washing machine", "dryer"),
    "air-conditioners": ("ac", "air conditioner", "air conditioners"),
    "monitors": ("monitor", "monitors", "odyssey", "viewfinity"),
    "audio-devices": ("audio", "soundbar", "soundbars", "earbuds", "buds", "sound tower"),
}


def is_brand_available_on_samsung(query: str | None) -> bool:
    if not query:
        return True
    q = query.strip().lower()
    words = set(re.findall(r"[a-z0-9]+", q))
    for brand in NON_SAMSUNG_BRANDS:
        b = brand.lower()
        if b in words or b == q or q.startswith(b + " ") or f" {b} " in f" {q} ":
            return False
    return True


def resolve_category(query: str | None) -> str | None:
    q = (query or "").strip().lower()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in q for k in kws):
            return cat
    return None


def sitemaps_for_category(category: str | None, base: str) -> list[str]:
    cat = (category or "").lower()
    base = base.rstrip("/")
    im = f"{base}/im-sitemap.xml"
    vd = f"{base}/vd-sitemap.xml"
    da = f"{base}/da-sitemap.xml"
    if cat in ("laptops", "smartphones", "tablets", "watches"):
        return [im]
    if cat in ("tvs", "monitors", "audio-devices"):
        return [vd]
    if cat in ("refrigerators", "washers-and-dryers", "air-conditioners"):
        return [da]
    return [im, vd, da]


def product_urls_from_sitemap(xml: str, base: str, category: str | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    root = base.rstrip("/")
    for link in re.findall(rf"<loc>({re.escape(root)}/[^\s<>]+)</loc>", xml):
        low = link.lower()
        if any(skip in low for skip in EXCLUDE_URL_PATTERNS):
            continue
        if category:
            seg = _category_segment(category)
            if seg and seg not in low:
                continue
        parts = [p for p in link.replace(root + "/", "").strip("/").split("/") if p]
        if len(parts) >= 2 and PRODUCT_MODEL_SUFFIX_REGEX.search(parts[-1]):
            clean = link.rstrip("/") + "/"
            if clean not in seen:
                seen.add(clean)
                urls.append(clean)
    return urls


def _category_segment(category: str | None) -> str | None:
    cat = (category or "").lower()
    mapping = {
        "laptops": "/computers/", "smartphones": "/smartphones/", "tablets": "/tablets/",
        "tvs": "/tvs/", "watches": "/watches/", "refrigerators": "/refrigerators/",
        "monitors": "/monitors/", "audio-devices": "/audio-devices/",
    }
    return mapping.get(cat)


def search_result_urls(html: str, base_url: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    root = base.rstrip("/")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if a.find_parent(["header", "nav", "footer"]):
            continue
        full = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        low = full.lower()
        if not low.startswith(root.lower()):
            continue
        if any(skip in low for skip in EXCLUDE_URL_PATTERNS):
            continue
        if not any(seg in low for seg in _PRODUCT_SEGMENTS):
            continue
        parts = [p for p in full.replace(root + "/", "").strip("/").split("/") if p]
        if len(parts) >= 2 and PRODUCT_MODEL_SUFFIX_REGEX.search(parts[-1]):
            clean = full.rstrip("/") + "/"
            if clean not in seen:
                seen.add(clean)
                urls.append(clean)
    return urls


_PRODUCT_TYPES = ("Product", "IndividualProduct", "ProductModel", "ProductGroup")


def _loads_ldjson(raw: str):
    """Samsung embeds unescaped control characters in its ld+json Product blocks,
    which strict json rejects; retry leniently before giving up."""
    try:
        return json.loads(raw)
    except Exception:
        try:
            return json.loads(raw, strict=False)
        except Exception:
            return None


def _json_ld_products(soup: BeautifulSoup) -> list[dict]:
    """Every Product / ProductGroup node in the page's ld+json. ProductGroup
    carries the Samsung variant family (hasVariant + variesBy)."""
    out: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        t = node.get("@type")
        types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
        if any(x in _PRODUCT_TYPES for x in types):
            out.append(node)
        if "@graph" in node:
            walk(node["@graph"])
        if "mainEntity" in node:
            walk(node["mainEntity"])

    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        data = _loads_ldjson(raw)
        if data is not None:
            walk(data)
    return out


def _product_group(products: list[dict]) -> dict | None:
    for p in products:
        t = p.get("@type")
        types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
        if "ProductGroup" in types and p.get("hasVariant"):
            return p
    return None


def _hidden_inputs(soup: BeautifulSoup) -> dict:
    """Samsung PDP commerce state is exposed as hidden <input> values: the
    Bazaarvoice passkey/api-url/product-id, the current model code & name, the
    shop SKU, the review totals and the price currency."""
    out: dict[str, str] = {}
    for inp in soup.find_all("input", attrs={"type": "hidden"}):
        iid = (inp.get("id") or "").strip()
        val = inp.get("value")
        if iid and val is not None and str(val).strip():
            out[iid] = str(val).strip()
    return out


_BUY_PATH_RE = re.compile(r"/buy/?$", re.I)
# A product slug trails the model name with colour / storage / model-code tokens
# (e.g. "galaxy-s24-ultra-titanium-gray-256gb-sm-s928bztcins"); these are peeled
# off to recover the canonical product slug ("galaxy-s24-ultra").
_SLUG_CODE_SUFFIX = re.compile(r"-sm-[a-z0-9]{6,}$", re.I)
_SLUG_STORAGE_SUFFIX = re.compile(r"-\d+\s*(?:gb|tb|mb)$", re.I)


def canonical_buy_url(url: str, base: str) -> str:
    """Map any discovered Samsung product URL to its canonical ``/buy/`` PDP.

    Samsung's search/sitemap surface deep variant URLs such as
    ``/smartphones/galaxy-s/galaxy-s24-ultra-titanium-gray-256gb-sm-s928bztcins/``
    which 302-redirect to the series landing page and expose no product data
    when visited as-is. Appending ``/buy/`` to that SAME full deep path (no
    shortening) reliably resolves to the live commercial PDP; Samsung itself
    normalizes the URL client-side afterwards. Reconstructing a shortened slug
    by stripping colour/storage/model-code tokens was tried and is fragile --
    marketing filler tokens (e.g. "5g", "awesome") vary by series and are not
    reliably distinguishable from meaningful ones (e.g. "ultra", "fe"), so an
    incomplete strip produces a URL Samsung 404s on. Always use the full path."""
    if not url:
        return url
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    if _BUY_PATH_RE.search(clean):
        return clean + "/"
    return clean + "/buy/"


def _clean_variant_text(s) -> str | None:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = s.replace("｜", " / ").replace("|", " / ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _variants(product_group: dict | None, hidden: dict, base_url: str) -> dict:
    """Variant family from the ld+json ProductGroup: one option per SKU with its
    colour, storage, canonical URL, model code and image. Shaped like the other
    platforms' ``variants`` dict ({label: [option dicts]})."""
    if not product_group:
        return {}
    name = _clean_variant_text(product_group.get("name")) or ""
    options: list[dict] = []
    seen: set[str] = set()
    for v in product_group.get("hasVariant") or []:
        if not isinstance(v, dict):
            continue
        sku = (v.get("sku") or "").strip() or None
        color = _clean_variant_text(v.get("color"))
        if color and color.islower():
            color = color.title()
        storage = None
        ap = v.get("additionalProperty")
        ap_list = ap if isinstance(ap, list) else ([ap] if isinstance(ap, dict) else [])
        for prop in ap_list:
            if isinstance(prop, dict) and str(prop.get("name", "")).lower() in ("storage", "capacity"):
                storage = _clean_variant_text(prop.get("value"))
        vname = _clean_variant_text(v.get("name"))
        # Descriptor without the repeated product name prefix.
        option = vname
        if option and name and option.lower().startswith(name.lower()):
            option = option[len(name):].strip(" -") or vname
        if not option:
            option = " ".join(x for x in (storage, color) if x) or sku
        url = v.get("@id") or v.get("url")
        if url and url.startswith("//"):
            url = "https:" + url
        image = v.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        key = (sku or option or "").lower()
        if key in seen:
            continue
        seen.add(key)
        options.append({
            "option": option,
            "color": color,
            "storage": storage,
            "product_id": sku,
            "url": url,
            "image": image.split("?")[0] if isinstance(image, str) else None,
        })
    if not options:
        return {}
    label = "Storage & Colour" if (product_group.get("variesBy") and
                                   len(product_group["variesBy"]) > 1) else "Variant"
    return {label: options}


def bazaarvoice_summary(data: dict, total_hint: int | None = None) -> dict | None:
    """Aggregate review statistics from a Bazaarvoice reviews.json response that
    was requested with ``Include=Products&Stats=Reviews``: average rating,
    distribution and per-aspect (secondary) rating averages."""
    if not isinstance(data, dict):
        return None
    products = ((data.get("Includes") or {}).get("Products")) or {}
    stats = None
    for p in products.values():
        if isinstance(p, dict) and isinstance(p.get("ReviewStatistics"), dict):
            stats = p["ReviewStatistics"]
            break
    total = data.get("TotalResults")
    if stats is None and total is None and total_hint is None:
        return None
    stats = stats or {}
    dist = {}
    for row in stats.get("RatingDistribution") or []:
        if isinstance(row, dict) and row.get("RatingValue") is not None:
            dist[f"{int(row['RatingValue'])}_star"] = row.get("Count")
    aspects = {}
    sra = stats.get("SecondaryRatingsAverages") or {}
    if isinstance(sra, dict):
        for key, val in sra.items():
            if isinstance(val, dict) and val.get("AverageRating") is not None:
                aspects[val.get("Label") or key] = {
                    "rating": round(_to_number(val.get("AverageRating")) or 0, 2) or None,
                }
    avg = _to_number(stats.get("AverageOverallRating"))
    total_reviews = stats.get("TotalReviewCount") or total or total_hint
    summary = {
        "average_rating": round(avg, 2) if avg is not None else None,
        "total_reviews": total_reviews,
        "total_ratings": total_reviews,
        "rating_breakdown": dist or None,
        "aspect_ratings": aspects or None,
        "total_photo_count": stats.get("TotalPhotoCount"),
        "total_video_count": stats.get("TotalVideoCount"),
        "recommended_count": stats.get("RecommendedCount"),
        "helpful_vote_count": stats.get("HelpfulVoteCount"),
    }
    return {k: v for k, v in summary.items() if v is not None} or None


def _to_number(raw):
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    cleaned = re.sub(r"[^\d.]", "", str(raw).replace(",", ""))
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def _product_slug(url: str) -> str | None:
    """The canonical product folder slug, e.g. ``galaxy-s24-ultra`` from
    ``/smartphones/galaxy-s24-ultra/buy/``."""
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    # Drop a trailing ``buy`` / ``specs`` leaf and any leading region/category.
    parts = [p for p in parts if p.lower() not in ("buy", "specs", "reviews")]
    return parts[-1] if parts else None


def _product_id(html: str, url: str, products: list[dict]) -> str:
    """Stable, readable product id: the canonical product slug (uppercased).
    Samsung variant SKUs differ per colour/storage, so the slug is the only
    family-stable identifier; SKUs are kept separately as the model number."""
    slug = _product_slug(url)
    if slug and not re.fullmatch(r"galaxy-s|galaxy-a|galaxy-z|smartphones", slug, re.I):
        return slug.upper()
    for p in products:
        if str(p.get("@type")) in ("ProductGroup",) and p.get("name"):
            return re.sub(r"[^A-Za-z0-9]+", "-", str(p["name"])).strip("-").upper()
    for p in products:
        for k in ("sku", "mpn", "productID", "gtin13", "gtin"):
            v = p.get(k)
            if v and str(v).strip():
                return str(v).strip().upper()
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    return parts[-1].upper() if parts else "UNKNOWN"


def _product_name(html: str, soup: BeautifulSoup, products: list[dict]) -> str | None:
    # Prefer the ProductGroup name, then any Product name.
    group = _product_group(products)
    if group and group.get("name"):
        return common.clean_text(str(group["name"]))
    for p in products:
        if p.get("name"):
            return common.clean_text(str(p["name"]))
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].split("|")[0].strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.split("|")[0].strip()
    return None


def _category(url: str, products: list[dict]) -> str | None:
    low = url.lower()
    if "samsung.com" in low:
        clean = re.sub(r"https?://[^/]+/[a-z]{2}/", "", url.split("?")[0]).strip("/")
        parts = clean.split("/")
        if parts:
            cat = parts[0].lower()
            if cat == "computers":
                return "laptops"
            if cat in ("smartphones", "tablets", "watches", "tvs", "monitors",
                       "refrigerators", "audio-devices", "washers-and-dryers", "air-conditioners"):
                return cat
    for p in products:
        cat = p.get("category")
        if isinstance(cat, str) and cat.strip():
            return re.split(r"[>/|]+", cat.strip().lower())[-1].strip()
    return None


def _price(html: str, soup: BeautifulSoup, products: list[dict], hidden: dict | None = None) -> dict:
    hidden = hidden or {}
    currency = (hidden.get("priceCurrency") or hidden.get("gPriceCurrency") or "INR").upper()
    selling = mrp = None
    availability = None

    # ld+json offers (most reliable when present).
    for p in products:
        offers = p.get("offers")
        offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
        for off in offer_list:
            if not isinstance(off, dict):
                continue
            if selling is None:
                selling = _to_number(off.get("price") or off.get("lowPrice"))
            if mrp is None:
                mrp = _to_number(off.get("highPrice"))
            if off.get("priceCurrency"):
                currency = str(off["priceCurrency"]).upper()
            avail = str(off.get("availability") or "").lower()
            if "instock" in avail or "limitedavailability" in avail:
                availability = "in_stock"
            elif "outofstock" in avail or "discontinued" in avail:
                availability = "out_of_stock"

    # Schema.org price on the commercial '/buy/' page:
    # <input itemprop="price" content="64999">. Samsung leaves this empty when
    # the exact SKU is no longer sold (EOL/out of stock) -- that is a genuine
    # absence, not an extraction failure, so selling stays None in that case.
    if selling is None:
        tag = soup.find(attrs={"itemprop": "price"})
        if tag:
            val = tag.get("content") or tag.get_text(strip=True)
            selling = _to_number(val)
        cur_tag = soup.find(attrs={"itemprop": "priceCurrency"})
        if cur_tag:
            cur_val = cur_tag.get("content") or cur_tag.get_text(strip=True)
            if cur_val:
                currency = cur_val.upper()

    # Rendered "MRP: ₹ 77,999.00 (Incl. of all taxes)" text alongside the
    # itemprop selling price is the list price on Samsung's redesigned buy page.
    if mrp is None:
        m = re.search(r"MRP:?\s*₹\s*([\d,]+(?:\.\d+)?)", html)
        if m:
            mrp = _to_number(m.group(1))

    # Price meta tags.
    if selling is None:
        tag = (soup.find("meta", attrs={"property": "product:price:amount"})
               or soup.find("meta", attrs={"property": "og:price:amount"}))
        if tag and tag.get("content"):
            selling = _to_number(tag["content"])

    # A selling + struck-through MRP pair ("₹ 35999 ₹ <s>79999</s>") is a strong
    # signal of the product's own price; a lone ₹ value on Samsung's buy page is
    # usually a cross-sell banner, so it is NOT trusted.
    if selling is None:
        for a, b in re.findall(r"₹\s*([\d,]{4,})\s*₹\s*(?:<s>|<del>|<strike>)?\s*([\d,]{4,})", html):
            sv, mv = _to_number(a), _to_number(b)
            if sv and mv and mv > sv >= 5000:
                selling, mrp = sv, mv
                break

    if mrp and selling and mrp == selling:
        mrp = None
    discount = None
    if mrp and selling and mrp > selling:
        discount = f"{round((mrp - selling) / mrp * 100)}%"

    # "Get at ₹ 31999*Plus, ₹1000 off with First Purchase Benefit" is the lowest
    # price reachable with stacked offers (exchange / first-purchase benefit).
    lowest_with_offers = None
    m = re.search(r"Get at\s*₹\s*([\d,]+(?:\.\d+)?)", html)
    if m:
        lowest_with_offers = _to_number(m.group(1))

    return {"currency": currency, "selling_price": selling, "mrp": mrp,
            "discount_percent": discount, "availability": availability,
            "lowest_with_offers": lowest_with_offers}


def _media(html: str, soup: BeautifulSoup, extra_images=None) -> dict:
    images: list[str] = []
    videos: list[str] = []
    seen: set[str] = set()

    def add(u):
        if not u:
            return
        u = str(u).strip()
        if u.startswith("//"):
            u = "https:" + u
        if "samsung.com" in u:
            u = u.split("?")[0]
        if not u.startswith("http"):
            return
        low = u.lower()
        if any(s in low for s in ("sprite", "icon", "logo", "arrow", "badge", "1x1", ".svg",
                                  "trustmark", "flag", "spinner", "placeholder")):
            return
        if not re.search(r"\.(?:jpg|jpeg|png|webp|gif|avif)\b", low) and "is/image" not in low:
            return
        if u not in seen and len(images) < 30:
            seen.add(u)
            images.append(u)

    # Variant / gallery images from structured sources (ld+json ProductGroup).
    for u in extra_images or []:
        add(u)
    # Samsung Scene7 product imagery: p6pim (gallery) and assets (buy/overview).
    # The current /buy/ pages serve /assets/... rather than /p6pim/..., so both
    # path families must be accepted (icons/sprites are filtered in add()).
    for raw in re.findall(
        r'(?:https:)?//images\.samsung\.com/is/image/samsung/[^"\'\s<>\\]+', html):
        add(raw)
    if not images:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            add(og["content"])
    for src in re.findall(r'src=["\']([^"\']+\.(?:mp4|webm))["\']', html, re.I):
        src = src.split("?")[0]
        full = src if src.startswith("http") else ("https:" + src if src.startswith("//") else src)
        if full.startswith("http") and full not in videos and len(videos) < 10:
            videos.append(full)
    return {"images": images, "videos": videos}


def _specifications(soup: BeautifulSoup, products: list[dict]) -> dict:
    specs: dict[str, dict] = {}

    def add(section, k, v):
        k = re.sub(r"\s+", " ", str(k or "")).strip().rstrip(":").strip()
        v = re.sub(r"\s+", " ", str(v or "")).strip()
        if not (2 <= len(k) <= 75 and 1 <= len(v) <= 240) or "{{" in k or "{{" in v:
            return
        if re.search(r"\b(price|purchase|offer|emi|cart|checkout|coupon|cashback|wishlist|buy now|add to)\b", k.lower()):
            return
        specs.setdefault(section or "General", {}).setdefault(k, v)

    for p in products:
        props = p.get("additionalProperty") or []
        if isinstance(props, dict):
            props = [props]
        for prop in props:
            if isinstance(prop, dict):
                add("General", prop.get("name"), prop.get("value"))

    for block in soup.select("div.pdd32-product-spec__item, [class*='specification__item']"):
        if block.find_parent(["header", "nav", "footer"]):
            continue
        h = block.select_one(".pdd32-product-spec__title button, .pdd32-product-spec__title, h3, h4")
        section = h.get_text(" ", strip=True) if h else "General"
        for li in block.select("li.pdd32-product-spec__content-item, li"):
            t = li.select_one("[class*='title'], [class*='name'], [class*='label'], [class*='key']")
            v = li.select_one("[class*='desc'], [class*='value'], [class*='detail']")
            if t and v and t is not v:
                add(section, t.get_text(" ", strip=True), v.get_text(" ", strip=True))

    for tr in soup.find_all("tr"):
        if tr.find_parent(["header", "nav", "footer"]):
            continue
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2:
            add("General", cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True))

    for dl in soup.find_all("dl"):
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            add("General", dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
    return specs


def flatten_specs(specs: dict) -> dict:
    return {f"{sec} | {k}": v for sec, rows in specs.items() for k, v in rows.items()}


def _introduced_date(html: str, soup: BeautifulSoup, products: list[dict]) -> str | None:
    raw = None
    for key in ("date", "publish-date", "article:published_time", "release_date"):
        tag = soup.find("meta", attrs={"name": key}) or soup.find("meta", attrs={"property": key})
        if tag and tag.get("content"):
            raw = tag["content"].strip()
            break
    if not raw:
        for p in products:
            for k in ("releaseDate", "datePublished", "dateCreated"):
                if p.get(k):
                    raw = str(p[k]).strip()
                    break
            if raw:
                break
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.split("T")[0]).isoformat()
    except ValueError:
        return raw


def _reviews_from_json_ld(products: list[dict]) -> tuple[list[dict], dict]:
    reviews: list[dict] = []
    agg = {"average_rating": None, "total_reviews": None}
    for p in products:
        a = p.get("aggregateRating")
        if isinstance(a, dict):
            if a.get("ratingValue") is not None:
                agg["average_rating"] = _to_number(a.get("ratingValue"))
            cnt = a.get("reviewCount") or a.get("ratingCount")
            if cnt is not None:
                try:
                    agg["total_reviews"] = int(cnt)
                except (ValueError, TypeError):
                    pass
        raw = p.get("review")
        rev_list = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for rev in rev_list:
            if not isinstance(rev, dict):
                continue
            rr = rev.get("reviewRating")
            val = rr.get("ratingValue") if isinstance(rr, dict) else rr
            author = rev.get("author")
            name = author.get("name") if isinstance(author, dict) else (str(author) if author else None)
            posted = rev.get("datePublished")
            rid = rev.get("@id") or rev.get("identifier")
            reviews.append({
                "review_id": str(rid) if rid else None,
                "rating": _to_number(val),
                "title": rev.get("name") or rev.get("headline"),
                "review_text": rev.get("reviewBody") or rev.get("description"),
                "reviewer_name": name,
                "reviewer_country": None,
                "verified_purchase": False,
                "posted_date": _norm_date(posted),
                "helpful_count": None,
            })
    return reviews, agg


def _norm_date(raw) -> str | None:
    if not raw:
        return None
    s = str(raw).split("T")[0].strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return common.iso_date(raw)


def _flat_find(flat: dict, *names) -> str | None:
    for want in names:
        for k, v in (flat or {}).items():
            if k.split(" | ", 1)[-1].strip().lower() == want.lower() and v:
                return v
    return None


def _valid_model_number(raw) -> str | None:
    if raw is None or isinstance(raw, bool):
        return None
    s = re.sub(r"\s+", " ", str(raw)).strip()
    if not s or s.lower() in _BAD_MODEL_VALUES or s.lower() in _COLOR_WORDS:
        return None
    if s[0] in "₹$€£" or re.fullmatch(r"[\d,]+(?:\.\d+)?%?", s):
        return None
    if not re.search(r"\d", s):
        return None
    if len(s) > 40 or len(s.split()) > 4:
        return None
    return s


def _model_number(html: str, url: str, products: list[dict], flat: dict, hidden: dict | None = None) -> str | None:
    hidden = hidden or {}
    # Samsung's own canonical model designation, then the shop SKU / variant SKU.
    for key in ("current_model_name", "shopSKU", "current_model_code"):
        v = _valid_model_number(hidden.get(key))
        if v:
            return v
    group = _product_group(products)
    if group:
        for v in (group.get("hasVariant") or []):
            if isinstance(v, dict):
                mv = _valid_model_number(v.get("sku"))
                if mv:
                    return mv
    for key in ("mpn", "sku", "productID", "gtin13"):
        for p in products:
            v = _valid_model_number(p.get(key))
            if v:
                return v
    path = url.split("?")[0]
    # Samsung codes often span two slug segments, e.g. "...-sm-r410nzkainu".
    m = re.search(r"-([a-z]{2,4}-[a-z0-9]*\d[a-z0-9]*)/?$", path, re.I)
    if not m:
        m = re.search(r"-([a-z]{1,4}\d{1,4}[a-z0-9]+)/?$", path, re.I)
    if m:
        v = _valid_model_number(m.group(1).upper())
        if v:
            return v
    return _valid_model_number(_flat_find(flat, "Model Number", "Model Code", "Model Name", "Model"))


def _color_from_text(text: str | None) -> str | None:
    if not text:
        return None
    words = re.findall(r"[A-Za-z]+", text)
    found = [w for w in words if w.lower() in _COLOR_WORDS]
    return found[-1].capitalize() if found else None


def _color(flat: dict, title: str | None, url: str | None,
           variants: dict | None = None, hidden: dict | None = None) -> str | None:
    spec = _flat_find(flat, "Colour", "Color", "Finish")
    if spec and not common.is_blank(spec):
        return common.clean_text(spec)
    # The colour of the selected (current) model code, else the first variant.
    hidden = hidden or {}
    current = hidden.get("current_model_code") or hidden.get("shopSKU")
    opts = []
    for lst in (variants or {}).values():
        if isinstance(lst, list):
            opts.extend(lst)
    if current:
        base = current.replace("INS", "")
        for o in opts:
            pid = (o.get("product_id") or "")
            if o.get("color") and (pid == current or pid.startswith(base) or base.startswith(pid)):
                return o["color"]
    for o in opts:
        if o.get("color"):
            return o["color"]
    return _color_from_text(title) or _color_from_text((url or "").replace("-", " "))


def _commerce(html: str, soup: BeautifulSoup, hidden: dict) -> dict:
    """Offer / payment / delivery / availability signals that Samsung's buy page
    exposes as benefit tiles and add-to-cart state (the price itself is rendered
    client-side and is not available in a reliable server source)."""
    def has(*words):
        return any(re.search(re.escape(w), html, re.I) for w in words)

    no_cost_emi = has("No Cost EMI")
    bank_offer = has("Instant Bank Cashback", "Bank Cashback")
    exchange = has("Exchange bonus", "Samsung exchange", "Trade-in", "Trade in")
    emi = no_cost_emi or has("Samsung Finance+ EMI", "No Cost EMI", "Finance+ EMI")
    free_delivery = has("Free Delivery")
    rewards = has("Samsung Rewards")
    assured_buyback = has("Assured Buyback")

    benefits = []
    for title in ("Instant Bank Cashback", "No Cost EMI", "Samsung Finance+ EMI",
                  "Samsung exchange", "Trade-in", "Samsung Rewards", "Samsung Care+",
                  "Assured Buyback", "Free Delivery", "Referral Advantage"):
        if has(title):
            benefits.append(title)
    offers = {
        "benefits": benefits or None,
        "no_cost_emi": no_cost_emi or None,
        "bank_offers": bank_offer or None,
        "exchange_offer": exchange or None,
        "emi_offers": emi or None,
        "samsung_rewards": rewards or None,
        "assured_buyback": assured_buyback or None,
    }
    offers = {k: v for k, v in offers.items() if v}

    # Availability: a live add-to-cart post handler means the product is buyable.
    availability = None
    if (hidden.get("addToCartPostYn") or "").upper() == "Y":
        availability = "in_stock"

    return {
        "offers": offers or None,
        "free_delivery": free_delivery,
        "availability": availability,
    }


def _selected_variant(variants: dict, hidden: dict) -> dict | None:
    current = hidden.get("current_model_code") or hidden.get("shopSKU")
    opts = [o for lst in (variants or {}).values() if isinstance(lst, list) for o in lst]
    if current:
        base = current.replace("INS", "")
        for o in opts:
            pid = o.get("product_id") or ""
            if pid and (pid == current or pid.startswith(base) or base.startswith(pid)):
                return o
    return opts[0] if opts else None


def _source_country(url: str | None) -> str | None:
    m = re.search(r"https?://[^/]+/([a-z]{2})(?:/|$)", url or "", re.I)
    return common.country_code(m.group(1)) if m else None


def _breadcrumbs(soup: BeautifulSoup) -> list[str]:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
            for g in graph:
                if not isinstance(g, dict):
                    continue
                t = g.get("@type")
                types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
                if "BreadcrumbList" not in types:
                    continue
                names: list[str] = []
                for it in g.get("itemListElement") or []:
                    if not isinstance(it, dict):
                        continue
                    nm = it.get("name") or (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
                    nm = common.clean_text(nm)
                    if nm and nm.lower() in ("moblie",):  # Samsung's own typo
                        nm = "Mobile"
                    if nm and nm.lower() not in ("home", "samsung", "buy", "specs", "reviews"):
                        names.append(nm)
                if names:
                    return names
    return []


def parse_product(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    products = _json_ld_products(soup)
    group = _product_group(products)
    hidden = _hidden_inputs(soup)
    specs = _specifications(soup, products)
    reviews, agg = _reviews_from_json_ld(products)
    variants = _variants(group, hidden, url)

    desc = None
    for p in products:
        if p.get("description"):
            desc = unescape(str(p["description"])).strip()
            break
    if not desc:
        md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
        if md and md.get("content"):
            desc = md["content"].strip()
    desc = common.clean_text(desc)

    # Variant imagery seeds the gallery so /buy/ pages (which serve /assets/...
    # rather than /p6pim/...) still yield product images.
    variant_images = [o.get("image") for lst in variants.values() for o in lst if o.get("image")]
    media = _media(html, soup, extra_images=variant_images)
    flat = flatten_specs(specs)
    name = _product_name(html, soup, products)

    # Fold variant storage/colour into specifications so they are not lost even
    # though the /buy/ page carries no full spec table.
    selected = _selected_variant(variants, hidden)
    if variants:
        for label, opts in variants.items():
            colours = sorted({o["color"] for o in opts if o.get("color")})
            storages = sorted({o["storage"] for o in opts if o.get("storage")})
            sec = specs.setdefault("General", {})
            if colours:
                sec.setdefault("Available Colours", ", ".join(colours))
            if storages:
                sec.setdefault("Available Storage", ", ".join(storages))
        # Selected variant's storage / RAM, so the single-value capacity attribute
        # (and the spec table) carry the configured device, not just the range.
        if selected and selected.get("storage"):
            parts = [p.strip() for p in str(selected["storage"]).split("/") if p.strip()]
            sec = specs.setdefault("General", {})
            if parts:
                sec.setdefault("Storage", parts[0])
            if len(parts) > 1:
                sec.setdefault("RAM", parts[1])

    commerce = _commerce(html, soup, hidden)

    total_reviews = None
    for k in ("BV-reviewTotalResult", "totalReviewCount"):
        if hidden.get(k) and str(hidden[k]).isdigit():
            total_reviews = int(hidden[k])
            break
    if total_reviews is not None:
        agg["total_reviews"] = total_reviews

    price = _price(html, soup, products, hidden)
    if not price.get("availability") and commerce.get("availability"):
        price["availability"] = commerce["availability"]
    delivery = {"free_delivery": True} if commerce.get("free_delivery") else {}

    return {
        "url": url,
        "product_id": _product_id(html, url, products),
        "model_number": _model_number(html, url, products, flat, hidden),
        "sku": hidden.get("shopSKU") or hidden.get("current_model_code"),
        "product_name": name,
        "brand": "Samsung",
        "category": _category(url, products),
        "color": _color(flat, name, url, variants, hidden),
        "source_country": _source_country(url),
        "breadcrumbs": _breadcrumbs(soup),
        "warranty_text": _flat_find(flat, "Warranty", "Warranty Period", "Warranty Information"),
        "description": desc,
        "price": price,
        "offers": commerce.get("offers") or {},
        "delivery": delivery,
        "ratings": agg,
        "specifications": specs,
        "specifications_flat": flatten_specs(specs),
        "images": media["images"],
        "videos": media["videos"],
        "variants": variants,
        "manufacturer": {"name": "Samsung", "generic_name": _category(url, products)},
        "introduced_date": _introduced_date(html, soup, products),
        "reviews": reviews,
        "seller": {"name": "Samsung Official Store"},
        # Bazaarvoice handles: the engine uses these to fetch ALL reviews + stats
        # without relying on the widget's own network request firing.
        "bv_passkey": hidden.get("BV-bvPassKey"),
        "bv_api_url": hidden.get("bvApiUrl"),
        "bv_product_id": hidden.get("current_model_code"),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_bazaarvoice(data: dict, product_id: str) -> list[dict]:
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for r in data.get("Results", []) or []:
        if not isinstance(r, dict):
            continue
        locale = (r.get("ContentLocale") or "").replace("-", "_").split("_")
        country = None
        if len(locale) > 1 and len(locale[1]) == 2:
            country = locale[1].upper()
        badges = r.get("Badges") or {}
        badge_low = " ".join(str(b).lower() for b in (badges.keys() if isinstance(badges, dict) else []))
        rid = r.get("Id") or r.get("ReviewId")
        # Customer-uploaded media attached to this review.
        images: list[str] = []
        for ph in (r.get("Photos") or []):
            if isinstance(ph, dict):
                sizes = ph.get("Sizes") or {}
                u = ((sizes.get("large") or {}).get("Url") or (sizes.get("normal") or {}).get("Url")
                     or (sizes.get("thumbnail") or {}).get("Url") or ph.get("Url"))
                if u:
                    images.append(u)
        videos: list[str] = []
        for vid in (r.get("Videos") or []):
            if isinstance(vid, dict):
                u = vid.get("VideoUrl") or vid.get("Url")
                if u:
                    videos.append(u)
        cdv = r.get("ContextDataValues") or {}
        variant = {k: (v.get("Value") if isinstance(v, dict) else v) for k, v in cdv.items()} or None
        sr = r.get("SecondaryRatings") or {}
        sub_ratings = {k: (v.get("Value") if isinstance(v, dict) else v) for k, v in sr.items()} or None
        out.append({
            "review_id": str(rid) if rid else None,
            "rating": _to_number(r.get("Rating")),
            "title": r.get("Title"),
            "review_text": r.get("ReviewText"),
            "reviewer_name": r.get("UserNickname"),
            "reviewer_country": country,
            "verified_purchase": "verified" in badge_low,
            "posted_date": _norm_date(r.get("SubmissionTime")),
            "helpful_count": r.get("TotalPositiveFeedbackCount"),
            "variant": variant,
            "sub_ratings": sub_ratings,
            "images": list(dict.fromkeys(images)),
            "videos": list(dict.fromkeys(videos)),
        })
    return out


def merge_reviews(jsonld: list[dict], bv: list[dict]) -> list[dict]:
    """Combine JSON-LD and Bazaarvoice reviews, preferring the richer Bazaarvoice
    entries (which carry media/all fields); de-dupe by review_id, else by
    reviewer + date + text."""
    def _key(r):
        rid = r.get("review_id")
        if rid:
            return ("id", str(rid))
        return ("c", (r.get("reviewer_name") or "").strip().lower(),
                str(r.get("posted_date") or ""),
                (r.get("review_text") or "").strip().lower()[:80])

    out, seen = [], set()
    for r in list(bv or []) + list(jsonld or []):
        k = _key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out
