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
    return unique(out)


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
    out["images"] = unique(imgs)
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
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


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
    if "instock" in r or "in_stock" in r or "available" in r:
        return "in_stock"
    if "outofstock" in r or "out_of_stock" in r or "unavailable" in r:
        return "out_of_stock"
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
