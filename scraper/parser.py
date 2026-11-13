"""
Universal Product Page Parser Module.
Supports Samsung India (digitalData + media + Bazaarvoice) AND multi-retail/generic
product pages via Schema.org JSON-LD, OpenGraph metadata, and common retail selectors.
"""

import logging
import re
import json
import mimetypes
from html import unescape
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urljoin, parse_qs
from bs4 import BeautifulSoup

from config import PLATFORM_CONFIGS
from models import PriceDetails
from scraper.filters import parse_date_string

logger = logging.getLogger(__name__)


def detect_platform_from_url(url: str) -> str:
    """Detects retail platform key from a product or catalog URL."""
    if not url:
        return "samsung"
    domain = (urlparse(url).netloc or "").lower()
    for p_key, cfg in PLATFORM_CONFIGS.items():
        p_domain = (cfg.get("domain") or "").lower()
        if p_domain and p_domain in domain:
            return p_key
    clean_domain = domain.replace("www.", "").split(":")[0]
    return clean_domain.split(".")[0] if clean_domain else "generic"


def is_date_within_past_year(date_str: Optional[str], days_cutoff: int = 365) -> bool:
    """
    Checks if a date string falls within the past `days_cutoff` days (default: 365).
    """
    dt = parse_date_string(date_str)
    if not dt:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_cutoff)
    return dt >= cutoff


def format_user_readable_date(date_str: Optional[str]) -> Optional[str]:
    """
    Converts ISO or raw date strings into human-readable format.
    Example: '2024-05-10' -> 'May 10, 2024'
    """
    if not date_str:
        return None
    dt = parse_date_string(date_str)
    if dt:
        return dt.strftime(f"%B {dt.day}, %Y")
    return date_str


def detect_mime_type(url: str) -> str:
    """Detects MIME types for image and video URLs."""
    clean_url = url.split("?")[0].lower()
    query = url.split("?")[1].lower() if "?" in url else ""

    if clean_url.endswith(".mp4") or "mp4" in clean_url or "mp4" in query:
        return "video/mp4"
    if clean_url.endswith(".webm") or "webm" in clean_url:
        return "video/webm"
    if clean_url.endswith(".m3u8") or "m3u8" in clean_url:
        return "application/x-mpegURL"

    if clean_url.endswith(".png") or "png" in query or "_png" in clean_url:
        return "image/png"
    if clean_url.endswith(".webp") or "webp" in query or "_webp" in clean_url:
        return "image/webp"
    if clean_url.endswith(".svg") or "svg" in query:
        return "image/svg+xml"
    if clean_url.endswith((".jpg", ".jpeg")) or "jpg" in query or "jpeg" in query or "_jpg" in clean_url:
        return "image/jpeg"

    guessed, _ = mimetypes.guess_type(clean_url)
    if guessed:
        return guessed
    return "image/jpeg" if "image" in url else "application/octet-stream"


def _walk_json_ld_products(node: Any, out: List[Dict[str, Any]]) -> None:
    """Recursively collects Schema.org Product nodes from JSON-LD payloads."""
    if isinstance(node, list):
        for item in node:
            _walk_json_ld_products(item, out)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("@type")
    type_list = [node_type] if isinstance(node_type, str) else (node_type if isinstance(node_type, list) else [])
    if any(t in ("Product", "IndividualProduct", "ProductModel") for t in type_list):
        out.append(node)

    if "@graph" in node:
        _walk_json_ld_products(node["@graph"], out)
    if "mainEntity" in node:
        _walk_json_ld_products(node["mainEntity"], out)


def extract_json_ld_products(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Extracts all Schema.org Product dicts embedded in JSON-LD script tags."""
    products: List[Dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or script.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            _walk_json_ld_products(data, products)
        except Exception:
            continue
    return products


def extract_product_id(
    html: str,
    url: str,
    soup: Optional[BeautifulSoup] = None,
    json_ld_products: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Extracts unique product ID / SKU / model code / ASIN across retail websites."""
    # 1. Samsung digitalData model_code
    dd_match = re.search(r'digitalData\.product\.model_code\s*=\s*["\']([^"\']+)["\']', html)
    if dd_match:
        code = dd_match.group(1).replace(r'\u002D', '-').replace(r'\u002d', '-').replace(r'\/', '-').replace('\\', '')
        if code.strip():
            return code.strip().upper()

    # 2. Schema.org JSON-LD identifiers
    for p_node in (json_ld_products or []):
        for key in ("sku", "mpn", "productID", "gtin13", "gtin"):
            val = p_node.get(key)
            if val and str(val).strip():
                return str(val).strip().upper()

    sku_match = re.search(r'"sku":\s*"([^"]+)"', html)
    if sku_match:
        return sku_match.group(1).strip().upper()

    # 3. Retail platform URL IDs (Amazon ASIN, BestBuy skuId, Alibaba/Walmart/Flipkart/JioMart/Croma)
    asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)', url, re.I)
    if asin_match:
        return asin_match.group(1).upper()

    parsed_qs = parse_qs(urlparse(url).query)
    for q_key in ("skuId", "pid", "itemId"):
        if q_key in parsed_qs and parsed_qs[q_key]:
            return parsed_qs[q_key][0].strip().upper()

    # Best Buy .p URL slug
    bb_match = re.search(r'/site/[^/]+/(\d+)\.p(?:[/?]|$)', url, re.I)
    if bb_match:
        return bb_match.group(1)

    # Alibaba product-detail numeric ID
    ali_match = re.search(r'/product-detail/[^/]+_(\d+)\.html(?:[/?]|$)', url, re.I)
    if ali_match:
        return ali_match.group(1)

    # Walmart /ip/.../<id>
    wm_match = re.search(r'/ip/(?:[^/]+/)?(\d+)(?:[/?]|$)', url, re.I)
    if wm_match:
        return wm_match.group(1)

    # JioMart / Croma / Reliance trailing numeric ID or /p/<id>
    p_tail_num = re.search(r'/p/.+?/(\d+)(?:[/?]|$)', url, re.I)
    if p_tail_num:
        return p_tail_num.group(1)

    retail_p_match = re.search(r'/p/([a-z0-9\-]+)(?:[/?]|$)', url, re.I)
    if retail_p_match:
        return retail_p_match.group(1).upper()

    # 4. Samsung-style URL slug suffix
    url_match = re.search(r'-([a-z]{1,4}\d{1,4}[a-z0-9\-]+|[a-z0-9]+-[a-z0-9]+)/?$', url.split("?")[0], re.I)
    if url_match:
        return url_match.group(1).upper()

    # 5. Fallback clean URL slug
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    return parts[-1].upper() if parts else "UNKNOWN-ID"


def extract_product_name(
    html: str,
    soup: BeautifulSoup,
    json_ld_products: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Extracts clean product name across any retail platform."""
    # 1. Samsung digitalData displayName
    dd_name = re.search(r'digitalData\.product\.displayName\s*=\s*["\']([^"\']+)["\']', html)
    if dd_name:
        clean = dd_name.group(1).encode("utf-8").decode("unicode_escape", errors="ignore")
        clean = clean.replace(r'\u002D', '-').replace(r'\u002d', '-').strip()
        if clean:
            return clean

    # 2. Schema.org Product name
    for p_node in (json_ld_products or []):
        if p_node.get("name"):
            return str(p_node["name"]).strip()

    # 3. Visible H1 on product page (preferred over clipped <title> on retail stores)
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)

    # 4. Meta og:title
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].split("|")[0].strip()

    if soup.title and soup.title.string:
        return soup.title.string.split("|")[0].strip()

    return "Unknown Product"


extract_laptop_name = extract_product_name


def extract_brand(
    html: str,
    soup: BeautifulSoup,
    platform: str,
    product_name: str = "",
    json_ld_products: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Extracts product brand dynamically (defaults to Samsung only on samsung.com)."""
    if platform == "samsung":
        return "Samsung"

    for p_node in (json_ld_products or []):
        brand_val = p_node.get("brand")
        if isinstance(brand_val, dict) and brand_val.get("name"):
            return str(brand_val["name"]).strip()
        if isinstance(brand_val, str) and brand_val.strip():
            return brand_val.strip()

    for attr_name in ("product:brand", "og:brand", "brand"):
        meta_b = soup.find("meta", attrs={"property": attr_name}) or soup.find("meta", attrs={"name": attr_name})
        if meta_b and meta_b.get("content"):
            return meta_b["content"].strip()

    known_brands = [
        "Samsung", "Apple", "HP", "Dell", "Lenovo", "Asus", "Acer", "MSI",
        "Sony", "LG", "OnePlus", "Xiaomi", "Redmi", "Realme", "Oppo", "Vivo",
        "Nothing", "Motorola", "Google", "Boat", "JBL", "Bose", "Whirlpool",
        "Haier", "Bosch", "Philips", "Microsoft", "Panasonic", "Hisense", "TCL"
    ]
    name_lower = (product_name or "").lower()
    for kb in known_brands:
        if re.search(rf'\b{re.escape(kb.lower())}\b', name_lower):
            return kb

    return "Unknown"


def extract_category(
    html: str,
    url: str,
    soup: Optional[BeautifulSoup] = None,
    json_ld_products: Optional[List[Dict[str, Any]]] = None,
    product_name: str = ""
) -> str:
    """Detects normalized product category across Samsung & third-party retail sites."""
    # 1. Samsung URL path categories
    if "samsung.com/in/" in url.lower():
        clean = url.split("?")[0].replace("https://www.samsung.com/in/", "").strip("/")
        parts = clean.split("/")
        if parts:
            cat = parts[0].lower()
            if cat in [
                "smartphones", "tablets", "watches", "tvs", "monitors",
                "refrigerators", "audio-devices", "washers-and-dryers",
                "air-conditioners", "microwave-ovens", "audio-sound", "rings"
            ]:
                return cat
            if cat == "computers":
                return "laptops"

    category_keywords = {
        "smartphones": ("smartphone", "iphone", "galaxy s", "galaxy z", "galaxy a", "mobile phone", "5g phone"),
        "laptops": ("laptop", "macbook", "notebook", "galaxy book", "ultrabook", "thinkpad", "vivobook", "computers"),
        "tablets": ("tablet", "ipad", "galaxy tab"),
        "tvs": ("television", "smart tv", "qled", "oled tv", "uhd tv", "led tv"),
        "watches": ("smartwatch", "apple watch", "galaxy watch", "fitness band"),
        "audio-devices": ("earbuds", "airpods", "headphone", "soundbar", "speaker", "buds", "music frame"),
        "refrigerators": ("refrigerator", "fridge", "double door", "side by side"),
        "monitors": ("monitor", "gaming monitor"),
        "washers-and-dryers": ("washing machine", "washer", "dryer"),
        "air-conditioners": ("air conditioner", "split ac", "inverter ac"),
    }

    # 2. JSON-LD category (normalize breadcrumb strings like 'Electronics > Computers > Laptops')
    for p_node in (json_ld_products or []):
        cat_val = p_node.get("category")
        if isinstance(cat_val, str) and cat_val.strip():
            cat_lower = cat_val.strip().lower()
            for cat_name, kws in category_keywords.items():
                if cat_lower == cat_name or any(kw in cat_lower for kw in kws):
                    return cat_name
            leaf = re.split(r'[>/|]+', cat_lower)[-1].strip()
            if leaf:
                return leaf

    # 3. Samsung digitalData
    m = re.search(r'digitalData\.product\.(?:pvi_subtype_name|pvi_type_name|category)\s*=\s*["\']([^"\']+)["\']', html)
    if m and m.group(1):
        return m.group(1).strip().lower()

    # 4. Keyword inference from URL/title for generic stores
    text_probe = f"{url} {product_name}".lower()
    for cat_name, kws in category_keywords.items():
        if any(kw in text_probe for kw in kws):
            return cat_name

    return "electronics"


def _parse_float_price(raw: Any) -> Optional[float]:
    """Safely converts raw price values like '₹1,29,999.00' or 129999 into float."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    cleaned = re.sub(r'[^\d.]', '', str(raw).replace(",", ""))
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def _extract_offer_details(json_ld_products: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[bool]]:
    """Extracts (current_price, original_price, currency, is_in_stock) from Schema.org Offer nodes."""
    for p_node in json_ld_products:
        offers = p_node.get("offers")
        offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
        for off in offer_list:
            if not isinstance(off, dict):
                continue
            cur = _parse_float_price(off.get("price") or off.get("lowPrice"))
            high = _parse_float_price(off.get("highPrice"))
            curr = str(off.get("priceCurrency") or "").strip().upper() or None
            avail = str(off.get("availability") or "").lower()
            in_stock = None
            if "instock" in avail or "limitedavailability" in avail:
                in_stock = True
            elif "outofstock" in avail or "discontinued" in avail:
                in_stock = False
            if cur is not None or high is not None:
                return cur, high, curr, in_stock
    return None, None, None, None


def extract_price_details(
    html: str,
    product_id: Optional[str] = None,
    laptop_name: Optional[str] = None,
    soup: Optional[BeautifulSoup] = None,
    json_ld_products: Optional[List[Dict[str, Any]]] = None,
    platform: str = "samsung"
) -> PriceDetails:
    """
    Extracts authentic price details directly from website metadata or DOM.
    Returns null values (None) if no active price is shown on the website.
    """
    current_price: Optional[float] = None
    original_price: Optional[float] = None
    default_currency: str = (PLATFORM_CONFIGS.get(platform or "samsung", {}).get("currency") or "INR").upper()
    currency: str = default_currency
    explicit_stock: Optional[bool] = None

    # 1. Samsung digitalData model_price and list_price
    cur_match = re.search(r'digitalData\.product\.model_price\s*=\s*["\']([^"\']+)["\']', html)
    list_match = re.search(r'digitalData\.product\.list_price\s*=\s*["\']([^"\']+)["\']', html)

    if cur_match:
        current_price = _parse_float_price(cur_match.group(1))
    if list_match:
        original_price = _parse_float_price(list_match.group(1))

    # 2. Schema.org JSON-LD offers
    if current_price is None and json_ld_products:
        o_cur, o_high, o_curr, o_stock = _extract_offer_details(json_ld_products)
        if o_cur is not None:
            current_price = o_cur
        if original_price is None and o_high is not None:
            original_price = o_high
        if o_curr:
            currency = o_curr
        if o_stock is not None:
            explicit_stock = o_stock

    # 3. OpenGraph / Meta product price tags
    if current_price is None and soup is not None:
        for prop in ("product:price:amount", "og:price:amount", "twitter:data1"):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                parsed_p = _parse_float_price(tag["content"])
                if parsed_p:
                    current_price = parsed_p
                    break
        curr_tag = soup.find("meta", attrs={"property": "product:price:currency"}) or soup.find("meta", attrs={"property": "og:price:currency"})
        if curr_tag and curr_tag.get("content"):
            currency = curr_tag["content"].strip().upper()

    # 4. Retail DOM price selectors fallback (for non-Samsung retail pages)
    if current_price is None and soup is not None and platform != "samsung":
        price_selectors = [
            "[itemprop='price']",
            "[data-testid='new-price']",
            ".pdp-price",
            ".new-price",
            ".a-price-whole",
            ".Nx9bqj",
            ".product-price",
            ".price"
        ]
        for sel in price_selectors:
            el = soup.select_one(sel)
            if el:
                raw_txt = el.get("content") or el.get_text(" ", strip=True)
                parsed_p = _parse_float_price(raw_txt)
                if parsed_p:
                    current_price = parsed_p
                    break

    # 5. Regex fallback on Schema string or visible rupee tag (kept strict for Samsung)
    if current_price is None:
        schema_price = re.search(r'"price":\s*"?(\d+(?:\.\d+)?)"?', html)
        if schema_price:
            current_price = _parse_float_price(schema_price.group(1))

    if current_price is None:
        rupee_match = re.search(r'₹\s*([\d,]+(?:\.\d+)?)', html)
        if rupee_match:
            val = _parse_float_price(rupee_match.group(1))
            if val and (platform != "samsung" or val > 10000):
                current_price = val
                currency = "INR"
        elif platform != "samsung" and default_currency == "USD":
            dollar_match = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', html)
            if dollar_match:
                val = _parse_float_price(dollar_match.group(1))
                if val:
                    current_price = val
                    currency = "USD"

    if original_price is None and current_price is not None:
        original_price = current_price

    in_stock = explicit_stock if explicit_stock is not None else (current_price is not None and current_price > 0)
    return PriceDetails(
        currency=currency,
        current_price=current_price,
        original_price=original_price,
        is_in_stock=in_stock
    )


def extract_introduction_date(
    html: str,
    soup: BeautifulSoup,
    first_reviewed_date: Optional[str] = None,
    json_ld_products: Optional[List[Dict[str, Any]]] = None
) -> Optional[str]:
    """Extracts release / introduction / publish date in human-readable format."""
    raw_date = None

    for meta_key in ("date", "publish-date", "article:published_time", "release_date"):
        tag = soup.find("meta", attrs={"name": meta_key}) or soup.find("meta", attrs={"property": meta_key})
        if tag and tag.get("content"):
            raw_date = tag["content"].strip()
            break

    if not raw_date and json_ld_products:
        for p_node in json_ld_products:
            for k in ("releaseDate", "datePublished", "dateCreated"):
                if p_node.get(k):
                    raw_date = str(p_node[k]).strip()
                    break
            if raw_date:
                break

    if not raw_date:
        dp_match = re.search(r'"(?:releaseDate|datePublished)":\s*"([^"]+)"', html)
        if dp_match:
            raw_date = dp_match.group(1)

    if not raw_date and first_reviewed_date:
        raw_date = first_reviewed_date

    return format_user_readable_date(raw_date)


def _parse_duration_seconds(raw_dur: Any) -> Optional[int]:
    """Parses ISO-8601 duration (e.g. PT1M30S) or numeric seconds to int."""
    if raw_dur is None:
        return None
    if isinstance(raw_dur, (int, float)):
        return int(raw_dur)
    s = str(raw_dur).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', s, re.I)
    if m:
        h = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        secs = int(m.group(3) or 0)
        total = h * 3600 + mins * 60 + secs
        return total if total > 0 else None
    return None


def _extract_json_ld_nodes_by_type(soup: BeautifulSoup, target_types: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Extracts all JSON-LD dict nodes matching any of the target @type names."""
    matched: List[Dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        n_type = node.get("@type")
        type_list = n_type if isinstance(n_type, list) else [n_type]
        if any(t in target_types for t in type_list if isinstance(t, str)):
            matched.append(node)
        if "@graph" in node:
            _walk(node["@graph"])

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            _walk(json.loads(raw))
        except Exception:
            continue
    return matched


def extract_media_v2(
    html: str,
    soup: Optional[BeautifulSoup] = None,
    base_url: str = "",
    json_ld_products: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extracts Schema v2.0.0 media objects:
      images: [{"url", "type", "position"}]
      videos: [{"video_id", "url", "thumbnail_url", "duration_seconds"}]
    """
    images: List[Dict[str, Any]] = []
    videos: List[Dict[str, Any]] = []
    seen_imgs = set()
    seen_vids = set()

    def _norm_url(raw_u: str) -> str:
        if not raw_u:
            return ""
        u = raw_u.strip()
        if u.startswith("//"):
            u = "https:" + u
        elif base_url and u.startswith("/"):
            u = urljoin(base_url, u)
        if "samsung.com" in u:
            u = u.split("?")[0]
        if not u.startswith("http"):
            return ""
        if any(skip in u.lower() for skip in ("sprite", "icon", "logo", "arrow", "badge", "1x1", ".svg")):
            return ""
        return u

    def _add_image(raw_u: str, img_type: Optional[str] = None) -> None:
        clean_u = _norm_url(raw_u)
        if not clean_u or clean_u in seen_imgs or len(images) >= 25:
            return
        seen_imgs.add(clean_u)
        pos = len(images) + 1
        inferred_type = img_type or ("main" if pos == 1 else "gallery")
        images.append({"url": clean_u, "type": inferred_type, "position": pos})

    def _add_video(
        raw_u: str,
        video_id: Optional[str] = None,
        thumb: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> None:
        clean_u = _norm_url(raw_u)
        if not clean_u or clean_u in seen_vids or len(videos) >= 10:
            return
        seen_vids.add(clean_u)
        videos.append({
            "video_id": video_id or f"vid_{len(videos) + 1:03d}",
            "url": clean_u,
            "thumbnail_url": _norm_url(thumb) or None if thumb else None,
            "duration_seconds": duration,
        })

    # 1. JSON-LD Product images & VideoObjects
    for p_node in (json_ld_products or []):
        imgs = p_node.get("image")
        if isinstance(imgs, str):
            _add_image(imgs)
        elif isinstance(imgs, list):
            for im in imgs:
                if isinstance(im, str):
                    _add_image(im)
                elif isinstance(im, dict) and (im.get("url") or im.get("contentUrl")):
                    _add_image(im.get("url") or im.get("contentUrl"))
        elif isinstance(imgs, dict) and (imgs.get("url") or imgs.get("contentUrl")):
            _add_image(imgs.get("url") or imgs.get("contentUrl"))

    if soup is not None:
        for v_node in _extract_json_ld_nodes_by_type(soup, ("VideoObject",)):
            v_url = v_node.get("contentUrl") or v_node.get("embedUrl") or ""
            _add_video(
                v_url,
                video_id=v_node.get("identifier"),
                thumb=v_node.get("thumbnailUrl") if isinstance(v_node.get("thumbnailUrl"), str) else None,
                duration=_parse_duration_seconds(v_node.get("duration")),
            )

    # 2. Samsung schema image & p6pim gallery assets
    schema_img = re.search(r'"image":\s*"(//images\.samsung\.com[^"]+|https://images\.samsung\.com[^"]+)"', html)
    if schema_img:
        _add_image(schema_img.group(1))

    img_matches = re.findall(
        r'["\'](//images\.samsung\.com/is/image/samsung/p6pim/[^"\'\s<>]+|\bhttps://images\.samsung\.com/is/image/samsung/p6pim/[^"\'\s<>]+)',
        html,
    )
    for raw_url in img_matches:
        _add_image(raw_url)
        if len(images) >= 25:
            break

    # 3. OpenGraph / generic gallery fallback
    if soup is not None and not images:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            _add_image(og_img["content"], "main")
        for img_tag in soup.find_all("img"):
            src = img_tag.get("data-src") or img_tag.get("src") or ""
            if any(ext in src.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
                _add_image(src)
                if len(images) >= 15:
                    break

    # 4. Videos from Samsung data-video-* & HTML5 <video> / .mp4 / .webm
    video_attrs = re.findall(r'data-video-[a-zA-Z0-9_\-]+="([^"]+)"', html)
    for raw_attr in video_attrs:
        try:
            v_data = json.loads(unescape(raw_attr))
            if isinstance(v_data, dict):
                _add_video(
                    v_data.get("desktopSrc") or v_data.get("mobileSrc") or "",
                    thumb=v_data.get("poster") or v_data.get("thumbnail"),
                )
        except Exception:
            pass

    for src in re.findall(r'src=["\']([^"\']+\.(?:mp4|webm))["\']', html, re.I):
        _add_video(src)

    return {"images": images, "videos": videos}


def extract_mime_media(
    html: str,
    soup: Optional[BeautifulSoup] = None,
    base_url: str = "",
    json_ld_products: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Backward-compatible wrapper around extract_media_v2."""
    media = extract_media_v2(html, soup=soup, base_url=base_url, json_ld_products=json_ld_products)
    items = [im["url"] for im in media["images"]] + [v["url"] for v in media["videos"]]
    if not items:
        return None
    return {
        "total_items": len(items),
        "images_count": len(media["images"]),
        "videos_count": len(media["videos"]),
        "items": items,
    }


def extract_identifiers(
    html: str,
    url: str,
    soup: BeautifulSoup,
    json_ld_products: List[Dict[str, Any]],
    base_product_id: str,
    platform: str,
) -> Dict[str, Optional[str]]:
    """Extracts Schema v2.0.0 product.identifiers authentically."""
    parent_id: Optional[str] = None
    sku: Optional[str] = None
    upc_ean: Optional[str] = None
    model_number: Optional[str] = None

    for p_node in json_ld_products:
        if not parent_id:
            in_group = p_node.get("inProductGroupWithID") or p_node.get("productGroupID")
            if isinstance(in_group, str) and in_group.strip():
                parent_id = in_group.strip()
            elif isinstance(p_node.get("isVariantOf"), dict):
                parent_id = p_node["isVariantOf"].get("productGroupID") or p_node["isVariantOf"].get("sku")
        if not sku and p_node.get("sku"):
            sku = str(p_node["sku"]).strip()
        if not upc_ean:
            for gtin_k in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin", "upc", "ean"):
                if p_node.get(gtin_k):
                    upc_ean = str(p_node[gtin_k]).strip()
                    break
        if not model_number:
            m_val = p_node.get("mpn") or p_node.get("model")
            if isinstance(m_val, dict):
                m_val = m_val.get("name") or m_val.get("mpn")
            if isinstance(m_val, str) and m_val.strip():
                model_number = m_val.strip()

    if not parent_id:
        m = re.search(r'"(?:parentAsin|parentProductId|productGroupId)"\s*:\s*"([A-Za-z0-9_\-]+)"', html)
        if m:
            parent_id = m.group(1)
    if not sku:
        m = re.search(r'"(?:sku|skuId)"\s*:\s*"([A-Za-z0-9_\-]+)"', html)
        if m:
            sku = m.group(1)
        elif platform == "samsung" and base_product_id:
            sku = base_product_id
    if not upc_ean:
        m = re.search(r'"(?:gtin13|gtin12|gtin|upc|ean)"\s*:\s*"(\d{8,14})"', html)
        if m:
            upc_ean = m.group(1)
    if not model_number:
        m = re.search(r'"(?:mpn|modelCode|modelNumber|model_number)"\s*:\s*"([A-Za-z0-9\-/]+)"', html)
        if m:
            model_number = m.group(1)
        elif platform == "samsung" and base_product_id and "-" in base_product_id:
            model_number = base_product_id

    return {
        "platform_product_id": base_product_id or None,
        "parent_product_id": parent_id,
        "sku": sku,
        "upc_ean": upc_ean,
        "model_number": model_number,
        "product_url": url,
    }


def extract_core_details(
    html: str,
    url: str,
    soup: BeautifulSoup,
    json_ld_products: List[Dict[str, Any]],
    platform: str,
    product_name: str,
    brand: str,
    category: str,
    introduced_date: Optional[str],
) -> Dict[str, Any]:
    """Extracts Schema v2.0.0 product.core (including breadcrumb category_path and condition)."""
    manufacturer: Optional[str] = None
    condition: str = "new"
    category_path: List[str] = []

    for p_node in json_ld_products:
        if not manufacturer and p_node.get("manufacturer"):
            mf = p_node["manufacturer"]
            manufacturer = mf.get("name") if isinstance(mf, dict) else str(mf)
        offers = p_node.get("offers")
        offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
        for off in offer_list:
            if not isinstance(off, dict):
                continue
            cond_raw = str(off.get("itemCondition") or "").lower()
            if "refurbish" in cond_raw:
                condition = "refurbished"
                break
            if "used" in cond_raw:
                condition = "used"
                break

    if "refurbish" in product_name.lower():
        condition = "refurbished"
    elif "renewed" in product_name.lower() and condition == "new":
        condition = "refurbished"

    if not manufacturer and platform == "samsung":
        manufacturer = "Samsung Electronics"

    # Extract breadcrumb category_path from JSON-LD BreadcrumbList first
    for bc_node in _extract_json_ld_nodes_by_type(soup, ("BreadcrumbList",)):
        elems = bc_node.get("itemListElement") or []
        if not isinstance(elems, list):
            continue
        sorted_elems = sorted(
            [e for e in elems if isinstance(e, dict)],
            key=lambda e: int(e.get("position") or 999),
        )
        temp_path: List[str] = []
        for el in sorted_elems:
            nm = el.get("name")
            if not nm and isinstance(el.get("item"), dict):
                nm = el["item"].get("name")
            if nm:
                nm_s = str(nm).strip()
                if nm_s.lower() not in ("home", "shop", "all", "") and nm_s.lower() != product_name.lower():
                    if nm_s not in temp_path:
                        temp_path.append(nm_s)
        if temp_path:
            category_path = temp_path
            break

    # HTML breadcrumb fallback
    if not category_path:
        bc_nav = soup.find("nav", attrs={"aria-label": re.compile(r"breadcrumb", re.I)}) or soup.find(
            class_=re.compile(r"breadcrumb", re.I)
        )
        if bc_nav:
            for a_tag in bc_nav.find_all(["a", "span", "li"]):
                if a_tag.find(["a", "li"]):
                    continue
                txt = a_tag.get_text(" ", strip=True)
                if txt and len(txt) < 60 and txt.lower() not in ("home", "shop", ">", "/") and txt.lower() != product_name.lower():
                    if txt not in category_path:
                        category_path.append(txt)

    if not category_path and category:
        category_path = [category]

    return {
        "title": product_name,
        "brand": brand,
        "manufacturer": manufacturer,
        "category_path": category_path,
        "condition": condition,
        "introduced_date": introduced_date,
    }


def extract_pricing_v2(
    price_details: Any,
    html: str,
    soup: BeautifulSoup,
    json_ld_products: List[Dict[str, Any]],
    platform: str,
) -> Dict[str, Any]:
    """Extracts Schema v2.0.0 pricing_and_availability fields authentically."""
    from dataclasses import asdict, is_dataclass

    pd_dict = (
        price_details
        if isinstance(price_details, dict)
        else (asdict(price_details) if is_dataclass(price_details) else {})
    )
    list_price = (
        pd_dict.get("mrp_price")
        if pd_dict.get("mrp_price") is not None
        else pd_dict.get("original_price")
    )
    current_price = pd_dict.get("current_price")
    discount_pct = pd_dict.get("discount_percentage")
    is_on_sale = bool(
        (list_price and current_price and list_price > current_price)
        or (discount_pct and discount_pct > 0)
    )

    moq: Optional[int] = None
    moq_m = re.search(r'"(?:moq|minOrderQuantity)"\s*:\s*(\d+)', html, re.I)
    if moq_m:
        try:
            moq = int(moq_m.group(1))
        except ValueError:
            pass
    elif re.search(r'Min\.?\s*Order\s*:?\s*(\d+)', soup.get_text(" ", strip=True), re.I):
        moq = int(re.search(r'Min\.?\s*Order\s*:?\s*(\d+)', soup.get_text(" ", strip=True), re.I).group(1))
    elif platform not in ("alibaba", "indiamart"):
        moq = 1

    # Free shipping & platform fulfillment signals (only when explicitly found)
    page_text_low = soup.get_text(" ", strip=True).lower()
    free_shipping: Optional[bool] = None
    is_fulfilled: Optional[bool] = None
    if platform == "samsung":
        is_fulfilled = True

    for p_node in json_ld_products:
        offers = p_node.get("offers")
        offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
        for off in offer_list:
            if isinstance(off, dict) and isinstance(off.get("shippingDetails"), dict):
                rate = (off["shippingDetails"].get("shippingRate") or {}).get("value")
                if rate is not None:
                    try:
                        free_shipping = float(rate) == 0.0
                    except (TypeError, ValueError):
                        pass

    if free_shipping is None and ("free shipping" in page_text_low or "free delivery" in page_text_low):
        free_shipping = True
    if is_fulfilled is None:
        if any(p in page_text_low for p in ("fulfilled by amazon", "flipkart assured", "sold & shipped by walmart", "sold and shipped by walmart")):
            is_fulfilled = True

    resolved_curr = (pd_dict.get("currency") or "USD").upper()
    no_cost_emi_available, emi_per_month, emi_offers = _extract_financing_offers(soup, resolved_curr)

    return {
        "currency": resolved_curr,
        "list_price": list_price,
        "current_price": current_price,
        "discount_percentage": discount_pct,
        "is_on_sale": is_on_sale,
        "b2b_price_tiers": [],
        "moq": moq,
        "in_stock": bool(pd_dict.get("is_in_stock", False)),
        "stock_status_text": pd_dict.get("stock_status_text"),
        "fulfillment": {
            "is_platform_fulfilled": is_fulfilled,
            "free_shipping": free_shipping,
            "estimated_delivery_days": None,
        },
        "financing": {
            "no_cost_emi_available": no_cost_emi_available,
            "emi_starting_per_month": emi_per_month,
            "no_cost_emi_offers": emi_offers,
        },
    }


def _extract_financing_offers(
    soup: BeautifulSoup,
    currency: str,
) -> Tuple[bool, Optional[float], List[str]]:
    """Extracts authentic No Cost EMI / 0% APR and monthly EMI snippets, ignoring template stubs."""
    # Remove scripts, styles, templates, and header/footer before scanning visible offer copy
    clean_chunks: List[str] = []
    for el in soup.find_all(["p", "span", "div", "li", "a", "strong"]):
        if el.find(["p", "div", "li"]):
            continue
        if el.find_parent(["script", "style", "template", "header", "nav", "footer"]):
            continue
        t = el.get_text(" ", strip=True)
        if not t or "{{" in t or "}}" in t or len(t) > 220:
            continue
        t_low = t.lower()
        if any(k in t_low for k in ("no cost emi", "no-cost emi", "0% apr", "0% interest", "emi starts", "emi available", "emi options", "/mo", "/ month")):
            clean_chunks.append(t)

    # Filter foreign-currency noise (e.g., Samsung cross-region preview blocks using £/$ on an INR page)
    curr = (currency or "INR").upper()
    other_sym = ("£", "€", "$") if curr == "INR" else (("₹", "£", "€") if curr == "USD" else ())
    expected_sym = "₹" if curr == "INR" else ("$" if curr == "USD" else curr)

    offers: List[str] = []
    seen = set()
    no_cost_emi = False
    monthly_candidates: List[float] = []

    for chunk in clean_chunks:
        c_low = chunk.lower()
        if any(sym in chunk for sym in other_sym) and expected_sym not in chunk:
            continue
        if re.search(r'\bno[\s\-]*cost\s*emi\b|\b0%\s*(?:apr|interest\s*emi)\b', c_low):
            no_cost_emi = True
            if chunk not in seen:
                seen.add(chunk)
                offers.append(chunk)
        elif re.search(r'(?:emi\s*(?:starts|from|options|available)|/mo\b|/\s*month\b)', c_low) and len(offers) < 6:
            if chunk not in seen:
                seen.add(chunk)
                offers.append(chunk)

        if any(k in c_low for k in ("emi", "/mo", "/ month", "per month")):
            for m in re.finditer(r'(?:₹|\$|rs\.?)\s*([\d,]+(?:\.\d{1,2})?)', chunk, re.I):
                try:
                    val = float(m.group(1).replace(",", ""))
                    if val > 0:
                        monthly_candidates.append(val)
                except ValueError:
                    pass

    emi_per_month = min(monthly_candidates) if monthly_candidates else None
    return no_cost_emi, emi_per_month, offers[:6]


def extract_content_and_specs(
    html: str,
    soup: BeautifulSoup,
    json_ld_products: List[Dict[str, Any]],
    platform: str,
) -> Dict[str, Any]:
    """Extracts short_description, bullet_features, specifications table, variants, and B2B flags."""
    short_desc: Optional[str] = None
    for p_node in json_ld_products:
        if p_node.get("description"):
            short_desc = unescape(str(p_node["description"])).strip()
            break
    if not short_desc:
        meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
        if meta_desc and meta_desc.get("content"):
            short_desc = meta_desc["content"].strip()
    if short_desc and len(short_desc) > 600:
        short_desc = short_desc[:597].rstrip() + "..."

    # Bullet features (excluding header/nav/footer/menu widgets)
    bullets: List[str] = []
    seen_b = set()
    _nav_noise_tokens = ("gnb", "navbar", "nav-", "menu", "footer", "header", "breadcrumb")
    for sel in (
        "#feature-bullets li",
        "[class*='key-feature'] li",
        "[class*='key_feature'] li",
        "[class*='product-highlight'] li",
        "[class*='highlight'] li",
        ".key-features li",
        "[class*='feature-benefit'] li",
        "[class*='selling-point'] li",
    ):
        for li in soup.select(sel):
            if li.find_parent(["header", "nav", "footer"]):
                continue
            anc_classes = " ".join(
                " ".join(p.get("class") or []) for p in li.parents if hasattr(p, "get")
            ).lower()
            if any(tok in anc_classes for tok in _nav_noise_tokens):
                continue
            txt = li.get_text(" ", strip=True)
            if 5 <= len(txt) <= 250 and txt not in seen_b:
                seen_b.add(txt)
                bullets.append(txt)
            if len(bullets) >= 12:
                break
        if bullets:
            break

    # Specifications grouped into section objects: {"Overview": {"Key": "Val"}, ...}
    specs: Dict[str, Dict[str, str]] = {}
    total_specs = 0
    max_specs = 120
    _spec_noise_keys = {"model name", "tenure", "interest", "bank", "emi", "monthly installment"}

    def _clean_section_name(raw_sec: Any) -> str:
        s = re.sub(r"\s+", " ", str(raw_sec or "")).strip().rstrip(":").strip()
        if not s or len(s) > 65 or "{{" in s:
            return "General"
        return s

    def _find_container_heading(node: Any) -> str:
        if node is None:
            return "General"
        cap = node.find("caption") if hasattr(node, "find") else None
        if cap:
            c_txt = cap.get_text(" ", strip=True)
            if c_txt and len(c_txt) <= 65:
                return _clean_section_name(c_txt)
        parent_block = node.find_parent(class_=re.compile(r"spec|detail|feature|accordion|item", re.I))
        if parent_block:
            hd = parent_block.select_one(
                ".pdd32-product-spec__title button, .pdd32-product-spec__title, "
                "[class*='spec__title'], [class*='spec-title'], h2, h3, h4, h5"
            )
            if hd:
                h_txt = hd.get_text(" ", strip=True)
                if h_txt and h_txt.lower() not in ("specifications", "product specifications"):
                    return _clean_section_name(h_txt)
        prev_h = node.find_previous(["h2", "h3", "h4"])
        if prev_h and not prev_h.find_parent(["header", "nav", "footer"]):
            p_txt = prev_h.get_text(" ", strip=True)
            if p_txt and len(p_txt) <= 65 and p_txt.lower() not in ("specifications", "product specifications"):
                return _clean_section_name(p_txt)
        return "General"

    def _add_spec(raw_k: Any, raw_v: Any, section: str = "General") -> None:
        nonlocal total_specs
        if total_specs >= max_specs:
            return
        k = re.sub(r"\s+", " ", str(raw_k or "")).strip().rstrip(":").strip()
        v = re.sub(r"\s+", " ", str(raw_v or "")).strip()
        if not (2 <= len(k) <= 75 and 1 <= len(v) <= 240):
            return
        if "{{" in k or "{{" in v or k.lower() in _spec_noise_keys:
            return
        if k.lower() == "model name" and v.lower() == "purchase price":
            return
        sec_name = _clean_section_name(section)
        sec_bucket = specs.setdefault(sec_name, {})
        if k not in sec_bucket:
            sec_bucket[k] = v
            total_specs += 1

    # 1. Schema.org Product JSON-LD additionalProperty
    for p_node in json_ld_products:
        add_props = p_node.get("additionalProperty") or []
        if isinstance(add_props, dict):
            add_props = [add_props]
        for prop in add_props:
            if isinstance(prop, dict):
                _add_spec(prop.get("name"), prop.get("value"), section="General")

    # 2. Samsung section-grouped specs (.pdd32-product-spec__item)
    for sec_block in soup.select("div.pdd32-product-spec__item, [class*='specification__item']"):
        if sec_block.find_parent(["header", "nav", "footer"]):
            continue
        h_el = sec_block.select_one(
            ".pdd32-product-spec__title button, .pdd32-product-spec__title, h3, h4"
        )
        sec_title = _clean_section_name(h_el.get_text(" ", strip=True) if h_el else "General")
        for item_li in sec_block.select("li.pdd32-product-spec__content-item, li"):
            t_el = item_li.select_one("[class*='title'], [class*='name'], [class*='label'], [class*='key']")
            v_el = item_li.select_one("[class*='desc'], [class*='value'], [class*='detail']")
            if t_el and v_el and t_el is not v_el:
                _add_spec(t_el.get_text(" ", strip=True), v_el.get_text(" ", strip=True), section=sec_title)
                continue
            direct_kids = [c for c in item_li.find_all(["p", "span", "div"], recursive=False) if c.get_text(strip=True)]
            if len(direct_kids) == 2:
                _add_spec(direct_kids[0].get_text(" ", strip=True), direct_kids[1].get_text(" ", strip=True), section=sec_title)

    # 3. General Samsung/retail spec items outside explicit section wrappers
    for item_li in soup.select(
        "li.pdd32-product-spec__content-item, [class*='spec-list'] li, [class*='specification'] li, .pd-specification li"
    ):
        if item_li.find_parent(["header", "nav", "footer"]) or item_li.find_parent(class_=re.compile(r"pdd32-product-spec__item|specification__item|cp-specification-info")):
            continue
        sec_title = _find_container_heading(item_li)
        t_el = item_li.select_one("[class*='title'], [class*='name'], [class*='label'], [class*='key'], dt, th")
        v_el = item_li.select_one("[class*='desc'], [class*='value'], [class*='detail'], dd, td")
        if t_el and v_el and t_el is not v_el:
            _add_spec(t_el.get_text(" ", strip=True), v_el.get_text(" ", strip=True), section=sec_title)
            continue
        direct_kids = [c for c in item_li.find_all(["p", "span", "div"], recursive=False) if c.get_text(strip=True)]
        if len(direct_kids) == 2:
            _add_spec(direct_kids[0].get_text(" ", strip=True), direct_kids[1].get_text(" ", strip=True), section=sec_title)

    # 4. Croma spec UL blocks (ul.cp-specification-info -> section header + ul.cp-specification-spec-info rows)
    for info_ul in soup.select("ul.cp-specification-info"):
        sec_hdr = None
        for child_li in info_ul.find_all("li", recursive=False):
            if not child_li.find("ul"):
                hdr_txt = child_li.get_text(" ", strip=True)
                if hdr_txt:
                    sec_hdr = hdr_txt
                    break
        if not sec_hdr:
            h_el = info_ul.find(["h3", "h4", "h5"])
            sec_hdr = h_el.get_text(" ", strip=True) if h_el else "General"
        for spec_ul in info_ul.select("ul.cp-specification-spec-info"):
            t_li = spec_ul.select_one("li.cp-specification-spec-title")
            v_li = spec_ul.select_one("li.cp-specification-spec-details")
            if t_li and v_li:
                _add_spec(t_li.get_text(" ", strip=True), v_li.get_text(" ", strip=True), section=sec_hdr)
    for ul in soup.select("ul.cp-specification-spec-info"):
        if ul.find_parent("ul", class_="cp-specification-info"):
            continue
        t_li = ul.select_one("li.cp-specification-spec-title")
        v_li = ul.select_one("li.cp-specification-spec-details")
        if t_li and v_li:
            _add_spec(t_li.get_text(" ", strip=True), v_li.get_text(" ", strip=True), section=_find_container_heading(ul))

    # 5. HTML tables (excluding header/nav/footer and EMI modals)
    for tr in soup.find_all("tr"):
        if tr.find_parent(["header", "nav", "footer"]):
            continue
        anc_cls = " ".join(" ".join(p.get("class") or []) for p in tr.parents if hasattr(p, "get")).lower()
        if any(skip in anc_cls for skip in ("emi", "finance", "calculator", "modal")):
            continue
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2:
            tbl = tr.find_parent("table")
            _add_spec(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True), section=_find_container_heading(tbl))

    # 6. Definition lists (<dl>)
    for dl in soup.find_all("dl"):
        if dl.find_parent(["header", "nav", "footer"]):
            continue
        sec_title = _find_container_heading(dl)
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            _add_spec(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True), section=sec_title)

    # 7. Amazon-style detail bullets (#detailBullets_feature_div li)
    for li in soup.select("#detailBullets_feature_div li"):
        raw_txt = re.sub(r"[\u200e\u200f]+", "", li.get_text(" ", strip=True))
        if ":" in raw_txt:
            k_part, v_part = raw_txt.split(":", 1)
            _add_spec(k_part, v_part, section="Product Details")

    included_items: List[str] = []
    for sec_dict in specs.values():
        if not isinstance(sec_dict, dict):
            continue
        for k, v in sec_dict.items():
            if any(w in k.lower() for w in ("in the box", "included", "package content")):
                parts = [p.strip() for p in re.split(r'[,;|]', v) if p.strip()]
                included_items = parts[:15]
                break
        if included_items:
            break

    # Schema.org ProductGroup hasVariant
    variants: List[Dict[str, Any]] = []
    for pg in _extract_json_ld_nodes_by_type(soup, ("ProductGroup",)):
        has_var = pg.get("hasVariant") or []
        if not isinstance(has_var, list):
            has_var = [has_var]
        for v_node in has_var[:25]:
            if not isinstance(v_node, dict):
                continue
            attrs: Dict[str, str] = {}
            for ak in ("color", "size", "material", "pattern"):
                if v_node.get(ak):
                    attrs[ak] = str(v_node[ak])
            v_offers = v_node.get("offers")
            first_off = v_offers[0] if isinstance(v_offers, list) and v_offers else (v_offers if isinstance(v_offers, dict) else {})
            v_price = _parse_float_price(first_off.get("price")) if isinstance(first_off, dict) else None
            avail_s = str(first_off.get("availability") or "").lower() if isinstance(first_off, dict) else ""
            variants.append({
                "variant_id": v_node.get("sku") or v_node.get("productID"),
                "attributes": attrs,
                "price": v_price,
                "in_stock": "instock" in avail_s if avail_s else None,
            })

    if platform in ("alibaba", "indiamart"):
        txt_low = soup.get_text(" ", strip=True).lower()
        b2b_cust = {
            "oem_odm_available": bool("oem" in txt_low or "odm" in txt_low),
            "custom_logo": bool("custom logo" in txt_low or "customized logo" in txt_low),
            "custom_packaging": bool("custom packaging" in txt_low or "customized packaging" in txt_low),
        }
    else:
        b2b_cust = {"oem_odm_available": False, "custom_logo": False, "custom_packaging": False}

    return {
        "short_description": short_desc,
        "bullet_features": bullets,
        "specifications": specs,
        "included_items": included_items,
        "variants": variants,
        "b2b_customization": b2b_cust,
    }


def extract_seller_info(
    html: str,
    url: str,
    soup: BeautifulSoup,
    json_ld_products: List[Dict[str, Any]],
    platform: str,
    brand: str = "",
) -> Dict[str, Any]:
    """Extracts authentic seller metadata from JSON-LD offers or platform signals."""
    from urllib.parse import urlparse
    from config import PLATFORM_CONFIGS

    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    store_url: Optional[str] = None
    is_official: Optional[bool] = None
    is_verified: Optional[bool] = None

    for p_node in json_ld_products:
        offers = p_node.get("offers")
        offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
        for off in offer_list:
            if not isinstance(off, dict):
                continue
            seller = off.get("seller")
            if isinstance(seller, dict):
                seller_name = seller.get("name") or seller_name
                store_url = seller.get("url") or store_url
                seller_id = seller.get("identifier") or seller_id

    if platform == "samsung":
        seller_name = seller_name or "Samsung Official Store"
        store_url = store_url or PLATFORM_CONFIGS.get("samsung", {}).get("home_url")
        is_official = True
        is_verified = True
    elif not seller_name:
        m = re.search(r'(?:Sold\s+by|Seller\s*:)\s*([A-Za-z0-9 &.,_\-]{2,60})', soup.get_text(" ", strip=True), re.I)
        if m:
            seller_name = m.group(1).strip()

    if is_official is None and seller_name and brand:
        if brand.lower() in seller_name.lower() or "official" in seller_name.lower():
            is_official = True

    if not store_url and seller_name:
        parsed_u = urlparse(url)
        if parsed_u.scheme and parsed_u.netloc and platform in ("samsung", "bestbuy", "target"):
            store_url = f"{parsed_u.scheme}://{parsed_u.netloc}"

    return {
        "seller_id": seller_id,
        "seller_name": seller_name,
        "store_url": store_url,
        "is_official_brand_store": is_official,
        "is_verified_seller": is_verified,
        "years_active": None,
        "seller_rating": None,
        "on_time_delivery_rate": None,
        "response_rate": None,
    }


def extract_reviews_v2_from_json_ld(
    json_ld_products: List[Dict[str, Any]],
    platform: str = "generic",
    platform_product_id: Optional[str] = None,
    parent_product_id: Optional[str] = None,
    seller_id: Optional[str] = None,
    days_cutoff: Optional[int] = 365,
    fallback_rating: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Extracts Schema v2.0.0 Review Details from Schema.org JSON-LD review/aggregateRating
    nodes for third-party retail websites, applying days_cutoff filtering when set.
    """
    from models import build_review_v2, normalize_iso_utc

    avg_rating: Optional[float] = None
    total_ratings: int = 0
    dist: Dict[str, int] = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
    items: List[Dict[str, Any]] = []
    earliest_date: Optional[str] = None
    cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=days_cutoff)) if (days_cutoff and days_cutoff > 0) else None

    for p_node in json_ld_products:
        agg = p_node.get("aggregateRating")
        if isinstance(agg, dict):
            val = agg.get("ratingValue")
            cnt = agg.get("reviewCount") or agg.get("ratingCount")
            if val is not None:
                try:
                    avg_rating = round(float(val), 2)
                except (ValueError, TypeError):
                    pass
            if cnt is not None:
                try:
                    total_ratings = int(cnt)
                except (ValueError, TypeError):
                    pass

        raw_reviews = p_node.get("review")
        rev_list = raw_reviews if isinstance(raw_reviews, list) else ([raw_reviews] if isinstance(raw_reviews, dict) else [])
        for idx, rev in enumerate(rev_list, start=1):
            if not isinstance(rev, dict):
                continue
            date_pub = rev.get("datePublished")
            if date_pub and (earliest_date is None or str(date_pub) < earliest_date):
                earliest_date = str(date_pub)

            if cutoff_dt and date_pub:
                dt = parse_date_string(str(date_pub))
                if dt and dt < cutoff_dt:
                    continue

            r_rating: Optional[float] = None
            rr = rev.get("reviewRating")
            raw_rv = rr.get("ratingValue") if isinstance(rr, dict) else rr
            if raw_rv is not None:
                try:
                    fv = float(raw_rv)
                    r_rating = int(fv) if fv.is_integer() else round(fv, 2)
                    sk = str(int(round(fv)))
                    if sk in dist:
                        dist[sk] += 1
                except (ValueError, TypeError):
                    pass

            author = rev.get("author")
            author_name = author.get("name") if isinstance(author, dict) else (str(author) if author else None)
            body = rev.get("reviewBody") or rev.get("description")

            items.append({
                "review_id": str(rev.get("@id") or rev.get("identifier") or f"LD_R_{idx}"),
                "platform_product_id": platform_product_id,
                "rating": r_rating,
                "title": rev.get("name") or rev.get("headline"),
                "body": body,
                "published_at": normalize_iso_utc(str(date_pub)) if date_pub else None,
                "language": rev.get("inLanguage"),
                "is_verified_purchase": None,
                "is_incentivized_or_default": False,
                "is_recommended": (r_rating >= 4) if r_rating is not None else None,
                "helpful_votes": None,
                "reviewer": {
                    "user_id": None,
                    "name": author_name,
                    "country_code": None,
                    "badges": [],
                },
                "purchased_variant": {"variant_id": None, "attributes_text": None},
                "tags": {"pros": [], "cons": []},
                "media": {"images": [], "videos": []},
                "comments": [],
            })

    if avg_rating is None and fallback_rating:
        avg_rating = fallback_rating.get("average_rating")
        if total_ratings == 0:
            total_ratings = int(fallback_rating.get("total_reviews") or 0)

    if items and total_ratings == 0:
        total_ratings = len(items)

    if avg_rating is None and total_ratings == 0 and not items:
        return None, earliest_date

    review_v2 = build_review_v2(
        platform=platform,
        platform_product_id=platform_product_id,
        parent_product_id=parent_product_id,
        seller_id=seller_id,
        average_rating=avg_rating,
        total_ratings=total_ratings,
        total_written_reviews=sum(1 for it in items if (it.get("body") or "").strip()),
        star_distribution=dist,
        product_review_items=items,
    )
    return review_v2, earliest_date


def extract_reviews_from_json_ld(
    json_ld_products: List[Dict[str, Any]],
    days_cutoff: Optional[int] = 365,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Backward-compatible alias returning Schema v2.0.0 Review Details from JSON-LD."""
    return extract_reviews_v2_from_json_ld(json_ld_products, days_cutoff=days_cutoff)


def extract_fallback_rating(
    html: str,
    json_ld_products: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Schema.org aggregate rating fallback."""
    for p_node in (json_ld_products or []):
        agg = p_node.get("aggregateRating")
        if isinstance(agg, dict) and agg.get("ratingValue") is not None:
            try:
                return {
                    "average_rating": float(agg["ratingValue"]),
                    "total_reviews": int(agg.get("reviewCount") or agg.get("ratingCount") or 0),
                }
            except (ValueError, TypeError):
                pass

    val = re.search(r'"ratingValue":\s*"?(\d+(?:\.\d+)?)"?', html)
    cnt = re.search(r'"(?:reviewCount|ratingCount)":\s*"?(\d+)"?', html)
    if val:
        return {
            "average_rating": float(val.group(1)),
            "total_reviews": int(cnt.group(1)) if cnt else 0,
        }
    return None


def parse_product_page(
    html: str,
    url: str,
    first_reviewed_date: Optional[str] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parses any Samsung or third-party retail product detail page
    and returns Schema v2.0.0 Product Details (`v2_product`) plus helper fields.
    """
    from models import build_product_v2

    soup = BeautifulSoup(html, "html.parser")
    resolved_platform = (platform if platform and platform not in ("auto", "all", "generic") else None) or detect_platform_from_url(url)
    json_ld_products = extract_json_ld_products(soup)

    product_id = extract_product_id(html, url, soup=soup, json_ld_products=json_ld_products)
    product_name = extract_product_name(html, soup, json_ld_products=json_ld_products)
    brand = extract_brand(html, soup, resolved_platform, product_name=product_name, json_ld_products=json_ld_products)
    category = extract_category(html, url, soup=soup, json_ld_products=json_ld_products, product_name=product_name)
    price_details = extract_price_details(
        html,
        product_id=product_id,
        laptop_name=product_name,
        soup=soup,
        json_ld_products=json_ld_products,
        platform=resolved_platform,
    )
    introduced_date = extract_introduction_date(
        html,
        soup,
        first_reviewed_date=first_reviewed_date,
        json_ld_products=json_ld_products,
    )
    identifiers = extract_identifiers(html, url, soup, json_ld_products, product_id, resolved_platform)
    core = extract_core_details(html, url, soup, json_ld_products, resolved_platform, product_name, brand, category, introduced_date)
    pricing_v2 = extract_pricing_v2(price_details, html, soup, json_ld_products, resolved_platform)
    content_specs = extract_content_and_specs(html, soup, json_ld_products, resolved_platform)
    media_v2 = extract_media_v2(html, soup=soup, base_url=url, json_ld_products=json_ld_products)
    seller_info = extract_seller_info(html, url, soup, json_ld_products, resolved_platform, brand=brand)
    fallback_rating = extract_fallback_rating(html, json_ld_products=json_ld_products)

    v2_product = build_product_v2(
        platform=resolved_platform,
        platform_product_id=identifiers["platform_product_id"],
        parent_product_id=identifiers["parent_product_id"],
        sku=identifiers["sku"],
        upc_ean=identifiers["upc_ean"],
        model_number=identifiers["model_number"],
        product_url=url,
        title=core["title"],
        brand=core["brand"],
        manufacturer=core["manufacturer"],
        category_path=core["category_path"],
        condition=core["condition"],
        introduced_date=core["introduced_date"],
        currency=pricing_v2["currency"],
        list_price=pricing_v2["list_price"],
        current_price=pricing_v2["current_price"],
        discount_percentage=pricing_v2["discount_percentage"],
        is_on_sale=pricing_v2["is_on_sale"],
        b2b_price_tiers=pricing_v2["b2b_price_tiers"],
        moq=pricing_v2["moq"],
        in_stock=pricing_v2["in_stock"],
        stock_status_text=pricing_v2["stock_status_text"],
        is_platform_fulfilled=pricing_v2["fulfillment"]["is_platform_fulfilled"],
        free_shipping=pricing_v2["fulfillment"]["free_shipping"],
        estimated_delivery_days=pricing_v2["fulfillment"]["estimated_delivery_days"],
        no_cost_emi_available=pricing_v2["financing"]["no_cost_emi_available"],
        emi_starting_per_month=pricing_v2["financing"]["emi_starting_per_month"],
        no_cost_emi_offers=pricing_v2["financing"]["no_cost_emi_offers"],
        short_description=content_specs["short_description"],
        bullet_features=content_specs["bullet_features"],
        specifications=content_specs["specifications"],
        included_items=content_specs["included_items"],
        variants=content_specs["variants"],
        oem_odm_available=content_specs["b2b_customization"]["oem_odm_available"],
        custom_logo=content_specs["b2b_customization"]["custom_logo"],
        custom_packaging=content_specs["b2b_customization"]["custom_packaging"],
        images=media_v2["images"],
        videos=media_v2["videos"],
        seller_id=seller_info["seller_id"],
        seller_name=seller_info["seller_name"],
        store_url=seller_info["store_url"],
        is_official_brand_store=seller_info["is_official_brand_store"],
        is_verified_seller=seller_info["is_verified_seller"],
        years_active=seller_info["years_active"],
        seller_rating=seller_info["seller_rating"],
        on_time_delivery_rate=seller_info["on_time_delivery_rate"],
        response_rate=seller_info["response_rate"],
    )

    return {
        "v2_product": v2_product,
        "platform": resolved_platform,
        "brand": brand,
        "product_id": product_id,
        "parent_product_id": identifiers["parent_product_id"],
        "seller_id": seller_info["seller_id"],
        "product_name": product_name,
        "laptop_name": product_name,
        "category": category,
        "product_url": url,
        "introduced_date": introduced_date,
        "price_details": price_details,
        "media": media_v2,
        "fallback_rating": fallback_rating,
        "json_ld_products": json_ld_products,
    }


parse_laptop_page = parse_product_page
