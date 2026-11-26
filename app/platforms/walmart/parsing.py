from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlencode, urlparse


def first_value(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def unique(values):
    out, seen = [], set()
    for v in values:
        if v is None:
            continue
        v = str(v).strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def dedupe_images(urls):
    """De-dupe product gallery images merged from several sources (PDP JSON
    ``imageInfo``, search-card thumbnails, raw DOM ``img.src``) that often
    point at the SAME photo asset but differ only by Walmart's ODN image
    CDN resize query string. Search/DOM thumbnails are served as
    ``...<path>.jpeg?odnWidth=180&odnHeight=180&odnBg=ffffff`` while the PDP
    gallery serves the identical path with no query string at all (full
    resolution). The dedup key is the URL path with the query string
    stripped; the first occurrence's position is kept, but a later hit for
    the same path WITHOUT a query string (i.e. the full-resolution form)
    upgrades an earlier query-string (resized) hit, so the output always
    prefers full resolution."""
    seen: dict[str, str] = {}
    order: list[str] = []
    for u in urls:
        if not u:
            continue
        u = str(u).strip()
        if not u:
            continue
        base, _, query = u.partition("?")
        key = base.lower()
        if key not in seen:
            seen[key] = base if query else u
            order.append(key)
        elif not query and seen[key] != base:
            seen[key] = base
    return [seen[k] for k in order]


def to_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not m:
        return None
    n = float(m.group(0).replace(",", ""))
    return int(n) if n.is_integer() else n


def find_values_by_key(obj, wanted_keys):
    found = []
    wanted = {k.lower() for k in wanted_keys}

    def walk(v):
        if isinstance(v, dict):
            for k, child in v.items():
                if str(k).lower() in wanted and child not in (None, "", [], {}):
                    found.append(child)
                walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)

    walk(obj)
    return found


def find_first_key(obj, wanted_keys):
    vals = find_values_by_key(obj, wanted_keys)
    return vals[0] if vals else None


def collect_media_urls(obj, ext_regex):
    urls = []

    def walk(v):
        if isinstance(v, dict):
            for child in v.values():
                walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)
        elif isinstance(v, str) and v.startswith("http") and re.search(ext_regex, v, re.I):
            urls.append(v)

    walk(obj)
    return unique(urls)


def search_url(query: str, page_no: int) -> str:
    return "https://www.walmart.com/search?" + urlencode({"q": query, "page": page_no})


def review_url(product_id: str, page_no: int, sort: str) -> str:
    return f"https://www.walmart.com/reviews/product/{product_id}?" + urlencode({"page": page_no, "sort": sort})


def page_props(script_text: str) -> dict:
    try:
        return json.loads(script_text).get("props", {}).get("pageProps", {})
    except Exception:
        return {}


def search_items(props: dict) -> list[dict]:
    if not isinstance(props, dict):
        return []
    stacks = ((props.get("initialData") or {}).get("searchResult") or {}).get("itemStacks") or []
    out, seen = [], set()
    for stack in stacks:
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items") or []:
            if not isinstance(item, dict):
                continue
            pid = first_value(item.get("usItemId"), item.get("id"))
            if not pid:
                continue
            pid = str(pid)
            if pid in seen:
                continue
            seen.add(pid)
            out.append(item)
    return out


def item_product_url(item: dict) -> str | None:
    canonical = first_value(item.get("canonicalUrl"), item.get("productUrl"))
    if canonical:
        return canonical if canonical.startswith("http") else "https://www.walmart.com/" + canonical.lstrip("/")
    pid = first_value(item.get("usItemId"), item.get("id"))
    return f"https://www.walmart.com/ip/{pid}" if pid else None


def item_images(item: dict) -> list[str]:
    out = []
    for v in find_values_by_key(item, ["thumbnailUrl", "url", "src", "imageUrl"]):
        if isinstance(v, str) and v.startswith("http") and re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", v, re.I):
            out.append(v)
        elif isinstance(v, dict):
            c = first_value(v.get("url"), v.get("src"))
            if c:
                out.append(c)
    return dedupe_images(out)


def item_price(item: dict):
    info = item.get("priceInfo") or {}
    current = info.get("currentPrice") or {}
    prange = info.get("priceRange") or {}
    raw = first_value(current.get("price"), current.get("priceString"), info.get("linePrice"),
                      prange.get("minPrice"), prange.get("priceString"), item.get("price"), item.get("priceString"))
    return to_number(raw)


def product_id_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"/ip/(?:[^/?#]+/)?(\d+)", urlparse(url).path)
    return m.group(1) if m else None


def base_product(item: dict, query: str) -> dict:
    return {
        "product_id": str(first_value(item.get("usItemId"), item.get("id"))),
        "product_name": first_value(item.get("name"), item.get("title")),
        "brand": first_value(item.get("brand"), item.get("brandName") if isinstance(item.get("brandName"), str) else None),
        "product_url": item_product_url(item),
        "price_amount": item_price(item),
        "average_rating": to_number(first_value(item.get("averageRating"), item.get("rating"))),
        "total_reviews": to_number(first_value(item.get("numberOfReviews"), item.get("ratingCount"))),
        "images": item_images(item),
        "search_query": query,
    }


def detail_fields(product_data: dict) -> dict:
    out = {}
    out["brand"] = first_value(product_data.get("brand"), product_data.get("brandName"))
    out["product_name"] = first_value(product_data.get("name"), product_data.get("title"))
    out["description"] = first_value(product_data.get("shortDescription"), product_data.get("description"),
                                     product_data.get("longDescription"))
    out["specifications"] = normalize_specs(first_value(
        product_data.get("specifications"), product_data.get("specification"), product_data.get("productAttributes")))
    info = product_data.get("imageInfo") or {}
    imgs = []
    for im in info.get("allImages") or []:
        if isinstance(im, dict):
            u = first_value(im.get("url"), im.get("src"))
            if u:
                imgs.append(u)
        elif isinstance(im, str):
            imgs.append(im)
    if info.get("thumbnailUrl"):
        imgs.append(info["thumbnailUrl"])
    out["images"] = dedupe_images(imgs)
    out["videos"] = collect_media_urls(product_data, r"\.(mp4|m3u8|webm)(\?|$)")
    price = (product_data.get("priceInfo") or {}).get("currentPrice") or {}
    out["price_amount"] = to_number(first_value(price.get("price"), price.get("priceString")))
    out["original_price"] = to_number(first_value(
        find_first_key(product_data, ["wasPrice", "listPrice"]),
        ((product_data.get("priceInfo") or {}).get("wasPrice") or {}).get("price")))
    out["average_rating"] = to_number(product_data.get("averageRating"))
    out["total_reviews"] = to_number(product_data.get("numberOfReviews"))
    out["availability"] = first_value(product_data.get("availabilityStatus"), product_data.get("availability"))
    out["model_number"] = first_value(product_data.get("model"), product_data.get("modelNumber"))

    variants = extract_variants(product_data)
    out["variants"] = variants
    out["color"] = first_value(
        selected_variant_option(variants, ["Actual Color", "Color", "Colour"]),
        find_spec(out["specifications"], "Color", "Colour", "Actual Color"),
    )
    capacity_spec = first_value(
        selected_variant_option(variants, ["Size", "Storage", "Capacity", "Count Per Pack"]),
        find_spec(out["specifications"], "Capacity", "Storage", "Size", "Total Storage Capacity"),
    )
    out["capacity"] = parse_capacity(capacity_spec)
    out["manufacturer"] = manufacturer_info(product_data, out["specifications"], out.get("brand"))
    out["delivery"] = delivery_info(product_data)
    out["warranty"] = warranty_info(product_data, out["specifications"])
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def find_spec(specs: dict, *names) -> str | None:
    """Case-insensitive lookup by exact label in the flat spec dict produced
    by ``normalize_specs`` (no section prefix, unlike Flipkart's specs)."""
    if not isinstance(specs, dict):
        return None
    wanted = {n.lower() for n in names}
    for k, v in specs.items():
        if str(k).strip().lower() in wanted and v not in (None, "", [], {}):
            return v if isinstance(v, str) else str(v)
    return None


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
        "kg": "kg", "kgs": "kg", "oz": "oz", "ct": "count", "pk": "pack",
    }.get(unit, unit)
    return {"value": to_number(m.group(1)), "unit": unit}


# Walmart's product-page JSON exposes multi-color/size items via a
# `variantCriteria` array: one entry per selectable attribute (e.g.
# `actual_color`, `size`), each carrying a `variantList` of selectable
# values (id/name/availabilityStatus/swatchImageUrl/products/selected).
# This shape (and the sibling `variantProductIdMap`/`variantsMap` fields)
# is confirmed via third-party Walmart-scraper documentation (Scrapfly,
# Apify axesso/ecomscrape actors); it could not be confirmed against a
# live page in this environment because Walmart's press-and-hold
# challenge blocked every attempt, including via a non-Playwright fetch.
def extract_variants(product_data: dict) -> dict:
    """Build a ``{criterion_label: [options]}`` map (same shape Flipkart's
    mapper consumes: a dict of named variant groups attached to the one
    master record, not separate records per color/size combo)."""
    out: dict = {}
    criteria = product_data.get("variantCriteria")
    if not isinstance(criteria, list):
        return out
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        label = first_value(criterion.get("name"), criterion.get("id"))
        if not label:
            continue
        label = str(label).replace("_", " ").strip().title()
        values = criterion.get("variantList") or criterion.get("values") or criterion.get("partyValues") or []
        if not isinstance(values, list):
            continue
        options = []
        for val in values:
            if not isinstance(val, dict):
                continue
            option_name = first_value(val.get("name"), val.get("value"), val.get("id"))
            if not option_name:
                continue
            entry = {"option": str(option_name), "selected": bool(val.get("selected"))}
            status = product_availability_status(first_value(val.get("availabilityStatus"), val.get("availability")))
            if status:
                entry["available"] = status == "in_stock"
            swatch = first_value(val.get("swatchImageUrl"), val.get("swatchImage"))
            if swatch:
                entry["swatch_image"] = swatch
            products = val.get("products") or val.get("usItemIds")
            if isinstance(products, list) and products:
                entry["product_ids"] = [str(p) for p in products if p]
            options.append(entry)
        if options:
            out[label] = options
    return out


def selected_variant_option(variants: dict, label_candidates) -> str | None:
    """Return the option text flagged ``selected`` for whichever of
    ``label_candidates`` is present in ``variants`` (case-insensitive)."""
    if not variants:
        return None
    lower_map = {k.lower(): k for k in variants}
    for cand in label_candidates:
        key = lower_map.get(cand.lower())
        if not key:
            continue
        for opt in variants[key]:
            if opt.get("selected"):
                return opt.get("option")
    return None


# `manufacturerName` is a top-level product-JSON field (confirmed via
# Scrapfly's documented field list); the spec-table fallback covers pages
# where only the specifications grid carries it. Not live-verified (see
# extract_variants note above).
def manufacturer_info(product_data: dict, specs: dict, brand) -> dict | None:
    name = first_value(
        product_data.get("manufacturerName"),
        find_spec(specs, "Manufacturer", "Manufactured By", "Manufacturer Name"),
    )
    country = find_spec(specs, "Country of Origin", "Country Of Origin")
    if not name and not country:
        return None
    out = {}
    if name:
        out["name"] = str(name).strip()
    elif brand:
        out["name"] = brand
    if country:
        out["country_of_origin"] = str(country).strip()
    return out or None


# Shipping/fulfillment text (e.g. "Free shipping, arrives ...") and a
# delivery-date estimate are confirmed real Walmart product-JSON
# concepts (`fulfillmentLabel`/`shippingText`, `fulfillmentSummary`/
# `deliveryDate`) per third-party scraper documentation, but their exact
# nesting under `product` could not be confirmed live, so this searches
# for the field BY KEY anywhere in the product JSON (same defensive
# pattern already used above for `wasPrice`/`listPrice`) rather than
# assuming one fixed path.
def delivery_info(product_data: dict) -> dict:
    out = {}
    text = first_value(
        find_first_key(product_data, ["shippingText"]),
        find_first_key(product_data, ["fulfillmentText"]),
    )
    date_raw = find_first_key(product_data, ["deliveryDate"])
    if text:
        text = str(text).strip()
        out["delivery_text"] = text
        out["free_delivery"] = bool(re.search(r"\bfree\b", text, re.I))
    if date_raw:
        out["estimated_delivery_date"] = parse_date(date_raw) or str(date_raw).strip()
    return out


# Manufacturer warranty duration/type, when Walmart shows it, lives either
# as a spec-table row ("Warranty Information" etc.) or under a `warranty`
# key on the item/offer JSON (per third-party scraper docs: a dict with a
# free-text `information` field, null when no warranty is advertised).
# Walmart's own "Walmart Protection Plan" (a paid add-on, not a
# manufacturer warranty) is a separate, session/pricing-dependent widget
# that could not be confirmed live or via research, so it is NOT
# extracted here to avoid fabricating a warranty_type for something this
# code can't actually confirm applies to the scraped product.
def warranty_info(product_data: dict, specs: dict) -> dict:
    out = {}
    raw = first_value(
        find_spec(specs, "Warranty", "Warranty Information", "Manufacturer Warranty", "Warranty Type"),
        find_first_key(product_data, ["warranty"]),
    )
    if isinstance(raw, dict):
        raw = first_value(raw.get("information"), raw.get("text"))
    if raw and isinstance(raw, str):
        m = re.search(r"(\d+)\s*[-\s]?\s*(year|yr|month)", raw, re.I)
        if m:
            n = int(m.group(1))
            out["duration_months"] = n * 12 if m.group(2).lower().startswith("y") else n
        if re.search(r"manufactur", raw, re.I):
            out["warranty_type"] = "manufacturer"
        out["warranty_text"] = raw.strip()
    return out


def normalize_specs(spec_values) -> dict:
    out = {}
    if isinstance(spec_values, dict):
        for k, v in spec_values.items():
            if v not in (None, "", [], {}):
                out[str(k).strip()] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    elif isinstance(spec_values, list):
        for item in spec_values:
            if not isinstance(item, dict):
                continue
            label = first_value(item.get("name"), item.get("label"), item.get("key"), item.get("title"))
            value = first_value(item.get("value"), item.get("displayValue"), item.get("values"), item.get("valueName"))
            if label and value not in (None, "", [], {}):
                out[str(label).strip()] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return out


def product_availability_status(raw) -> str | None:
    r = (raw or "").lower()
    # Check the negative case first: "unavailable" contains "available" as a
    # literal substring, so checking in-stock first would misclassify it.
    if "outofstock" in r or "out_of_stock" in r or "unavailable" in r or "sold out" in r:
        return "out_of_stock"
    if "instock" in r or "in_stock" in r or "available" in r:
        return "in_stock"
    return None


def find_review_array(obj) -> list:
    for cand in find_values_by_key(obj, ["customerReviews", "reviews", "reviewList", "reviewItems"]):
        if isinstance(cand, list) and cand and isinstance(cand[0], dict):
            if any(k in cand[0] for k in ("reviewText", "reviewTitle", "reviewId", "rating")):
                return cand
    return []


def parse_review(raw: dict) -> dict:
    photos = []
    for p in raw.get("photos") or raw.get("reviewPhotos") or []:
        if isinstance(p, str):
            photos.append(p)
        elif isinstance(p, dict):
            u = first_value(p.get("url"), ((p.get("sizes") or {}).get("normal") or {}).get("url"), p.get("src"))
            if u:
                photos.append(u)
    badges = []
    for b in raw.get("badges") or []:
        if isinstance(b, str):
            badges.append(b)
        elif isinstance(b, dict):
            v = first_value(b.get("text"), b.get("badgeType"), b.get("id"))
            if v:
                badges.append(v)
    return {
        "review_id": first_value(raw.get("reviewId"), raw.get("id")),
        "reviewer_name": first_value(raw.get("userNickname"), raw.get("reviewerName"), raw.get("userName")),
        "rating": to_number(raw.get("rating")),
        "title": first_value(raw.get("reviewTitle"), raw.get("title")),
        "review_text": first_value(raw.get("reviewText"), raw.get("text")),
        "posted_date": parse_date(first_value(raw.get("reviewSubmissionTime"), raw.get("submissionTime"), raw.get("date"))),
        "verified_purchase": any("verified" in str(x).lower() for x in badges),
        "helpful_count": to_number(first_value(raw.get("positiveFeedback"), raw.get("helpfulVotes"))),
        "photos": unique(photos),
    }


def parse_date(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            stamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def rating_from_text(body_text: str) -> dict:
    out = {"average_rating": None, "total_reviews": None}
    norm = re.sub(r"\s+", " ", body_text or "").strip()
    avg = re.search(r"([0-5](?:\.\d+)?)\s*out\s*of\s*5", norm, re.I)
    if avg:
        out["average_rating"] = float(avg.group(1))
    counts = (re.search(r"([\d,]+)\s+ratings?\s*[|·•/]\s*([\d,]+)\s+reviews?", norm, re.I)
              or re.search(r"([\d,]+)\s+ratings?\s+([\d,]+)\s+reviews?", norm, re.I))
    if counts:
        out["total_reviews"] = int(counts.group(2).replace(",", ""))
    return out
