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
    m = re.search(r"(\d{5,})", urlparse(url).path)
    return m.group(1) if m else None


def _safe_url(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get("url") or obj.get("contentUrl") or obj.get("thumbnailUrl")
    return None


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
        "average_rating": None, "total_reviews": None,
    }
    seen_img, seen_vid = set(), set()

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
                if u and u not in seen_img:
                    seen_img.add(u)
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
    return out


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
    return reviews


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
