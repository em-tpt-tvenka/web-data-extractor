

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.platforms.common import clean_text, country_code, dedupe, iso_date

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
REVIEW_ID_RE = re.compile(r"^R[A-Z0-9]{8,}$")

_CURRENCY_SYMBOLS = (("A$", "AUD"), ("C$", "CAD"), ("CA$", "CAD"), ("₹", "INR"),
                     ("£", "GBP"), ("€", "EUR"), ("$", "USD"))

_BLOCK_MARKERS = (
    "/errors/validateCaptcha", "Enter the characters you see below",
    "Type the characters you see in this image", "api-services-support@amazon.com",
)


# ── Generic helpers ──────────────────────────────────────────────────────────

def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def _text(el) -> str | None:
    return clean_text(el.get_text(" ", strip=True)) if el else None


def _first_text(root, *selectors) -> str | None:
    for sel in selectors:
        t = _text(root.select_one(sel))
        if t:
            return t
    return None


def to_number(s) -> float | None:
    if s is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    return float(m.group().replace(",", "")) if m else None


def to_int(s) -> int | None:
    n = to_number(s)
    return int(n) if n is not None else None


def full_size_image(url: str | None) -> str | None:
    """Amazon image URL without size modifiers: abc._SS40_.jpg -> abc.jpg"""
    if not url:
        return url
    return re.sub(r"\._[^/]*?_?\.(jpe?g|png|webp|gif)$", r".\1", url, flags=re.I)


def _decode_after(html: str, pattern: str):
    """Decode the JSON value that follows a regex match (handles nesting)."""
    dec = json.JSONDecoder()
    for m in re.finditer(pattern, html):
        try:
            return dec.raw_decode(html, m.end())[0]
        except ValueError:
            continue
    return None


def looks_blocked(html: str) -> bool:
    return any(marker in (html or "") for marker in _BLOCK_MARKERS)


# Amazon's throttling page ("Oops! It's rush hour and traffic is piling up on
# that page. Please try again in a short while.", HTTP 503) - a rate limit for
# this session / IP, not a CAPTCHA and not an empty result.
_THROTTLE_MARKERS = (
    "It's rush hour and traffic is piling up", "It&#39;s rush hour and traffic is piling up",
    "rush hour and traffic is piling up", "503 - Service Unavailable Error",
)


_NOT_FOUND_MARKERS = ("not a functioning page on our site", "Looking for something?")


def looks_not_found(html: str | None, url: str = "", status: int | None = None) -> bool:
    """Amazon's "Page Not Found" page ("We're sorry. The Web address you entered
    is not a functioning page on our site."). On a valid review-listing URL this
    is how Amazon refuses review pages to a rate-limited session (directly or
    via a redirect to ``/ref=cs_404_link``)."""
    if status == 404 or "cs_404" in (url or ""):
        return True
    return any(marker in (html or "") for marker in _NOT_FOUND_MARKERS)


def looks_throttled(html: str | None, status: int | None = None) -> bool:
    if status in (429, 503):
        return True
    return any(marker in (html or "") for marker in _THROTTLE_MARKERS)


def is_signin_page(html: str, url: str = "") -> bool:
    return "/ap/signin" in (url or "") or 'id="ap_email"' in (html or "") \
        or 'name="signIn"' in (html or "")


# ── Search results ───────────────────────────────────────────────────────────

def parse_search_page(html: str, base_url: str) -> tuple[list[dict], str | None]:
    """Result cards from a search page, plus the next-page URL (or None)."""
    soup = soup_of(html)
    items: list[dict] = []
    for div in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = (div.get("data-asin") or "").strip()
        if not ASIN_RE.match(asin):
            continue
        title = _first_text(div, "[data-cy='title-recipe'] h2", "h2 a span", "h2")
        brand_line = _first_text(div, "[data-cy='title-recipe'] h2 + h2", ".s-line-clamp-1 span")
        if brand_line and title and brand_line != title and not title.lower().startswith(brand_line.lower()):
            title = f"{brand_line} {title}"
        items.append({
            "asin": asin,
            "title": title,
            "is_sponsored": bool(
                div.select_one(".puis-sponsored-label-text, .s-sponsored-label-text, "
                               "[aria-label='View Sponsored information or leave ad feedback']")
            ) or "AdHolder" in (div.get("class") or []),
            "price_display": _first_text(div, ".a-price:not(.a-text-price) .a-offscreen"),
            "rating_text": _first_text(div, "i.a-icon-star-small .a-icon-alt", "span.a-icon-alt"),
        })
    nxt = soup.select_one("a.s-pagination-next:not(.s-pagination-disabled)")
    next_url = urljoin(base_url + "/", nxt["href"]) if nxt and nxt.get("href") else None
    return items, next_url


# ── Product page: specifications ─────────────────────────────────────────────

# (section label, row selectors) in precedence order: the first table that
# yields a key wins. Rows inside an Amazon expander take the expander's own
# header ("Display", "Item details", ...) as their section instead.
_SPEC_TABLE_SECTIONS = (
    ("Technical Details", ("#productDetails_techSpec_section_1 tr", "#productDetails_techSpec_section_2 tr",
                           "#technicalSpecifications_section_1 tr")),
    ("Additional Information", ("#productDetails_detailBullets_sections1 tr",)),
    ("Product Details", ("#productDetails_expanderTables_depthLeftSections tr",
                         "#productDetails_expanderTables_depthRightSections tr",
                         "[id^='productDetails_expanderSectionTables'] tr")),
    ("Technical Details", ("table.prodDetTable tr", "#prodDetails table tr", "#tech table tr")),
    ("Product Overview", ("#productOverview_feature_div table tr", "#poExpander table tr")),
)
_SKIP_SPEC_KEYS = {"customer reviews", "best sellers rank"}


def _expander_section(el) -> str | None:
    box = el.find_parent(class_=re.compile(r"\ba-expander-container\b"))
    head = box.select_one(".a-expander-header, .a-expander-prompt") if box else None
    return _text(head)


def extract_spec_sections(soup) -> dict[str, dict[str, str]]:
    """Specifications grouped by the page's own sections (Flipkart-style
    ``{"Section": {"Key": "Value"}}``). A key is kept once, in the first
    section it appears in."""
    sections: dict[str, dict[str, str]] = {}
    seen: set[str] = set()

    def put(section, k, v):
        k, v = clean_text(k), clean_text(v)
        if k:
            k = k.rstrip(":").strip()
        if k and v and k not in seen and k.lower() not in _SKIP_SPEC_KEYS:
            seen.add(k)
            sections.setdefault(section, {})[k] = v

    for label, selectors in _SPEC_TABLE_SECTIONS:
        for sel in selectors:
            for tr in soup.select(sel):
                cells = tr.find_all(["th", "td"], recursive=False)
                if len(cells) >= 2:
                    put(_expander_section(tr) or label, _text(cells[0]), _text(cells[-1]))
    for li in soup.select("#detailBullets_feature_div li span.a-list-item"):
        key_el = li.select_one("span.a-text-bold")
        full = _text(li)
        if key_el and full:
            key = _text(key_el) or ""
            put("Product Details", key, full[len(key):].lstrip(" :"))
    for cell in soup.select("#glance_icons_div td, #glance_icons_div .a-column"):
        k = cell.select_one("span.a-text-bold")
        v = cell.select_one("span.handle-overflow:not(.a-text-bold), span.a-size-base:not(.a-text-bold)")
        if k and v:
            put("Product Overview", _text(k), _text(v))
    return sections


def extract_specifications(soup) -> dict[str, str]:
    """All specification rows as one ``{key: value}`` map (no sections)."""
    return {k: v for rows in extract_spec_sections(soup).values() for k, v in rows.items()}


def flatten_specs(sections: dict) -> dict[str, str]:
    """Same shape as Flipkart's ``specifications_flat``: ``"Section | Key"``."""
    return {f"{sec} | {k}": v for sec, rows in sections.items() for k, v in rows.items()}


def spec_get(specs: dict, *keys: str) -> str | None:
    lower = {k.lower(): v for k, v in specs.items()}
    return next((lower[k.lower()] for k in keys if lower.get(k.lower())), None)


_MFG_SPEC_KEYS = {
    "manufactured_by": ("Manufacturer", "Manufacturer Name", "Manufacturer Details", "Manufactured By"),
    "marketed_by": ("Marketed By", "Marketer"),
    "packed_by": ("Packer", "Packed By", "Packer Details"),
    "imported_by": ("Importer", "Imported By", "Importer Details"),
    "country_of_origin": ("Country of Origin",),
    "generic_name": ("Generic Name",),
    "net_quantity": ("Net Quantity",),
    "manufacturing_date": ("Manufacturing Date", "Date First Available"),
}


def extract_manufacturer(specs: dict, brand: str | None = None) -> dict | None:
    """Manufacturer / packer / importer details in Flipkart's ``manufacturer``
    shape, read only from Amazon's specification rows."""
    out: dict[str, str] = {}
    for key, names in _MFG_SPEC_KEYS.items():
        v = spec_get(specs, *names)
        if v:
            # Amazon often repeats the entity ("Lenovo, Lenovo"); keep each part once.
            out[key] = ", ".join(dedupe([x.strip() for x in v.split(",")])) if key.endswith("_by") else v
    if not out:
        return None
    name = out.get("manufactured_by") or out.get("marketed_by") or brand
    if name:
        out["name"] = name.split(",")[0].strip()
    return out


def extract_variants(html: str, base_url: str) -> dict[str, list[dict]]:
    """Twister variations in Flipkart's ``variants`` shape:
    ``{"Colour": [{"option": "Grey", "url": ".../dp/ASIN", "product_id": "ASIN"}]}``.
    Read from the page's embedded twister data, never guessed."""
    out: dict[str, list[dict]] = {}
    by_asin = _decode_after(html, r'"dimensionValuesDisplayData"\s*:\s*')
    # Current Amazon twister: dimension labels come from variationDisplayLabels
    # ({"size_name": "Size", "color_name": "Colour", ...}); the dimension ORDER
    # (to line up with each ASIN's value list) comes from variationValues keys.
    labels_map = _decode_after(html, r'"variationDisplayLabels"\s*:\s*')
    var_values = _decode_after(html, r'"variationValues"\s*:\s*')
    dim_keys = list(var_values.keys()) if isinstance(var_values, dict) else []
    if isinstance(by_asin, dict) and dim_keys and isinstance(labels_map, dict):
        for asin, values in by_asin.items():
            if not ASIN_RE.match(str(asin)) or not isinstance(values, list):
                continue
            for idx, value in enumerate(values):
                if idx >= len(dim_keys):
                    break
                dim_key = dim_keys[idx]
                label = clean_text(labels_map.get(dim_key)
                                   or re.sub(r"_name$", "", str(dim_key)).replace("_", " ").title())
                value = clean_text(value)
                if not label or not value:
                    continue
                opts = out.setdefault(label, [])
                if all(o["option"] != value for o in opts):
                    opts.append({"option": value, "url": f"{base_url}/dp/{asin}", "product_id": asin})
    # Legacy twister: dimensionsDisplay (ordered label list) + dimensionValuesDisplayData.
    if not out:
        labels = _decode_after(html, r'"dimensionsDisplay"\s*:\s*')
        if isinstance(labels, list) and isinstance(by_asin, dict):
            for asin, values in by_asin.items():
                if not ASIN_RE.match(str(asin)) or not isinstance(values, list):
                    continue
                for label, value in zip(labels, values):
                    label, value = clean_text(label), clean_text(value)
                    if not label or not value:
                        continue
                    opts = out.setdefault(label, [])
                    if all(o["option"] != value for o in opts):
                        opts.append({"option": value, "url": f"{base_url}/dp/{asin}", "product_id": asin})
    # Last resort: variationValues ({dim_key: [values]}) without per-ASIN urls.
    if not out and isinstance(var_values, dict):
        for key, vals in var_values.items():
            label = clean_text((labels_map or {}).get(key)) \
                or re.sub(r"_name$", "", str(key)).replace("_", " ").title()
            opts = [{"option": clean_text(v), "url": None, "product_id": None}
                    for v in dedupe(vals if isinstance(vals, list) else []) if isinstance(v, str)]
            if opts:
                out[label] = opts
    return {k: v for k, v in out.items() if v}


# ── Product page: pricing ────────────────────────────────────────────────────

def _price_from(soup, selectors) -> str | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        t = _text(el)
        if not t:
            box = el.find_parent(class_="a-price") or el
            # The whole part embeds a decimal span ("2,804 ."); drop it before joining.
            whole = re.sub(r"[\s.]", "", _text(box.select_one(".a-price-whole")) or "")
            frac = re.sub(r"\D", "", _text(box.select_one(".a-price-fraction")) or "")
            sym = _text(box.select_one(".a-price-symbol")) or ""
            if whole:
                t = f"{sym}{whole}.{frac}" if frac else f"{sym}{whole}"
        if t and to_number(t) is not None:
            return t
    return None


_ISO_CURRENCY_RE = re.compile(r"(?<![A-Z])(INR|USD|GBP|EUR|CAD|AUD|AED|JPY|CNY|SGD|SAR|MXN|BRL)(?![A-Z])")


def currency_of(display: str | None, default: str) -> str:
    """Currency as displayed. Amazon may show prices converted to the visitor's
    currency (e.g. 'INR2,804.10' on amazon.com), so an ISO code on the page wins
    over the marketplace default."""
    if display:
        m = _ISO_CURRENCY_RE.search(display.upper())
        if m:
            return m.group(1)
        for sym, code in _CURRENCY_SYMBOLS:
            if sym in display:
                return code
    return default


def extract_pricing(soup, default_currency: str) -> dict:
    current = _price_from(soup, (
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#priceblock_dealprice", "#priceblock_ourprice", "#price_inside_buybox",
    ))
    original = _price_from(soup, (
        "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-text-price[data-a-strike='true'] .a-offscreen",
        "#corePrice_desktop .a-text-price .a-offscreen",
        "#corePrice_feature_div .a-text-price .a-offscreen",
        "#listPrice", "#priceblock_listprice",
    ))
    if not original:
        box = _text(soup.select_one(
            "#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #apex_desktop")) or ""
        m = re.search(r"(?:M\.R\.P\.?|List Price|Typical price|Was)\s*:?\s*([₹$£€]\s?[\d,]+(?:\.\d+)?)",
                      box, re.I)
        original = m.group(1) if m else None

    cp, op = to_number(current), to_number(original)
    if op is not None and cp is not None and op <= cp:
        op, original = None, None  # a "was" price that is not higher is not a discount
    disc = to_number(_first_text(soup, "#corePriceDisplay_desktop_feature_div .savingsPercentage",
                                 ".savingsPercentage"))
    if disc is None and cp and op:
        disc = round((op - cp) / op * 100, 1)
    return {
        "currency": currency_of(current or original, default_currency),
        "current_price": cp,
        "original_price": op,
        "discount_percentage": abs(disc) if disc is not None and op else None,
        "discount_amount": round(op - cp, 2) if cp is not None and op else None,
        "price_display": current,
        "original_price_display": original,
    }


# ── Product page: seller, delivery, offers ───────────────────────────────────

def extract_seller(soup) -> dict:
    sold_by = _first_text(
        soup, "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
        "#sellerProfileTriggerId",
        "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Sold by']")
    ships_from = _first_text(
        soup, "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message",
        "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Ships from']")
    combined = _text(soup.select_one("#merchantInfoFeature_feature_div")) or ""
    if not ships_from and sold_by and re.search(r"Shipper\s*/\s*Seller", combined, re.I):
        ships_from = sold_by  # the page labels one entity as both shipper and seller
    merchant = _text(soup.select_one("#merchant-info")) or ""
    if not ships_from and re.search(r"fulfilled by amazon|ships from and sold by amazon", merchant, re.I):
        ships_from = "Amazon"
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

    fulfilled_by_amazon = None
    if ships_from:
        fulfilled_by_amazon = "amazon" in ships_from.lower()
    elif re.search(r"fulfilled by amazon", merchant, re.I):
        fulfilled_by_amazon = True
    return {"seller_name": sold_by, "seller_id": seller_id, "ships_from": ships_from,
            "fulfilled_by_amazon": fulfilled_by_amazon}


def extract_delivery(soup) -> dict:
    timed = soup.select_one("[data-csa-c-delivery-time]")
    estimated = clean_text(timed.get("data-csa-c-delivery-time")) if timed else None
    if not estimated:
        estimated = _text(soup.select_one(
            "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE .a-text-bold"))
    cost = None
    priced = soup.select_one("[data-csa-c-delivery-price]")
    if priced:
        p = priced.get("data-csa-c-delivery-price") or ""
        cost = 0.0 if "free" in p.lower() else to_number(p)
    if cost is None:
        msg = _text(soup.select_one("#mir-layout-DELIVERY_BLOCK, #deliveryBlockMessage")) or ""
        if re.match(r"\s*FREE\b", msg, re.I) or re.search(r"\bFREE delivery\b", msg):
            cost = 0.0
    return {"estimated_delivery": estimated, "delivery_cost": cost}


def extract_offers(soup) -> list[dict]:
    offers = []
    for card in soup.select("#vsxoffers_feature_div .a-carousel-card, div.offers-items, div[id^='itembox-']"):
        title = _first_text(card, ".offers-items-title", "h6", ".a-text-bold")
        detail = _first_text(card, ".a-truncate-full", ".offers-items-content", "span.a-size-base")
        if title and detail:
            offers.append({"offer_type": title, "offer_details": detail})
    out, seen = [], set()
    for o in offers:
        key = (o["offer_type"], o["offer_details"])
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def extract_badges(soup) -> list[str]:
    """Service icons under the price: replacement, warranty, pay on delivery..."""
    out = []
    for sel in ("#iconfarmv2_feature_div .icon-content", "#iconfarmv2_feature_div a.a-size-small",
                "#iconfarmv2_feature_div span.a-size-small", "#icon-farm-container .a-size-small",
                "#productSupportAndReturnPolicy_feature_div .a-size-small",
                "#productSupportAndReturnPolicy_feature_div a", "#RETURNS_POLICY span"):
        out += [_text(e) for e in soup.select(sel)]
    return dedupe([b for b in out if b and len(b) < 120])


def group_offers(offers: list[dict], payment: dict, promotion: dict, exchange_text: str | None) -> dict:
    """Amazon offer cards grouped into Flipkart's ``offers`` structure.
    Each entry is ``"<offer type> | <offer details>"``."""
    def line(o):
        return f"{o['offer_type']} | {o['offer_details']}"

    bank = [line(o) for o in offers if re.search(r"\bbank\b", o["offer_type"], re.I)]
    emi = [line(o) for o in offers if re.search(r"\bEMI\b", o["offer_type"], re.I)]
    used = set(bank) | set(emi)
    return {
        "bank_offers": bank,
        "emi_offers": emi,
        "instant_loan_offers": [],
        "exchange_offer": exchange_text or "",
        "additional_benefits": [line(o) for o in offers if line(o) not in used],
        "no_cost_emi_mentioned": bool(payment.get("no_cost_emi")),
        "coupon_mentioned": bool(promotion.get("coupon_available")),
        "all_offers": offers,
    }


def payment_signals(offers: list[dict], badges: list[str], soup) -> dict:
    """True only on explicit page evidence; None when the page says nothing."""
    blob = " | ".join([f"{o['offer_type']} {o['offer_details']}" for o in offers] + badges)
    emi_box = _text(soup.select_one("#inemi_feature_div, #emiFeature_feature_div")) or ""
    exchange_box = _text(soup.select_one("#buyBackAccordionRow, #tradeInBuyboxFeature_feature_div")) or ""

    def flag(pattern, *texts):
        return True if any(re.search(pattern, t or "", re.I) for t in texts) else None

    return {
        "emi_available": flag(r"\bEMI\b", blob, emi_box),
        "no_cost_emi": flag(r"no[\s-]*cost\s+EMI", blob, emi_box),
        "bank_offer_available": flag(r"\bbank offer\b", blob),
        "exchange_offer_available": flag(r"\bexchange\b", blob, exchange_box),
        "cash_on_delivery": flag(r"pay on delivery|cash on delivery", blob),
    }


def _deal_label(soup) -> str | None:
    for el in soup.select("#dealBadge_feature_div, #dealBadgeSupportingText, .dealBadge"):
        for hidden in el.select(".aok-hidden, .a-hidden, script, style"):
            hidden.decompose()
        m = re.search(r"([A-Z][A-Za-z ]{0,40}?\bdeal)\b", _text(el) or "", re.I)
        if m:
            return clean_text(m.group(1))
    return None


def extract_promotion(soup, offers: list[dict]) -> dict:
    deal = _deal_label(soup)
    coupon = _first_text(soup, "#promoPriceBlockMessage_feature_div label[id^='couponText']",
                         "#vpcButton", "#couponBadgeRegularVpc", "[id^='couponText']")
    if coupon and "coupon" not in coupon.lower():
        coupon = None
    if coupon:
        coupon = clean_text(re.sub(r"\s*(Terms|Details|Shop items)\s*$", "", coupon, flags=re.I))
    if not coupon:
        coupon = next((o["offer_details"] for o in offers if "coupon" in o["offer_type"].lower()), None)
    if deal and not re.search(r"deal", deal, re.I):
        deal = None
    promo_type = "deal" if deal else ("coupon" if coupon else None)
    return {
        "is_promotional": bool(deal or coupon),
        "promotion_type": promo_type,
        "promotion_name": deal or coupon,
        "coupon_available": True if coupon else None,
    }


# ── Product page: availability ───────────────────────────────────────────────

def parse_availability(text: str | None) -> dict:
    if not text:
        return {"status": None, "stock_quantity": None}
    t = text.lower()
    m = re.search(r"only\s+(\d+)\s+left", t)
    if m:
        return {"status": "limited_stock", "stock_quantity": int(m.group(1))}
    if re.search(r"cannot be shipped to your selected delivery location|does not ship to", t):
        return {"status": "not_deliverable_to_location", "stock_quantity": None}
    if re.search(r"currently unavailable|out of stock|unavailable", t):
        return {"status": "out_of_stock", "stock_quantity": None}
    if re.search(r"pre-?order", t):
        return {"status": "preorder", "stock_quantity": None}
    if re.search(r"in stock|available to ship|usually dispatched|ships within", t):
        return {"status": "in_stock", "stock_quantity": None}
    return {"status": None, "stock_quantity": None}


# ── Product page: media ──────────────────────────────────────────────────────

def extract_media(soup, html: str) -> dict:
    images, thumbs = [], []
    initial = _decode_after(html, r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*")
    if isinstance(initial, list):
        for img in initial:
            if isinstance(img, dict):
                images.append(img.get("hiRes") or img.get("large"))
                thumbs.append(img.get("thumb"))
    if not any(images):
        land = soup.select_one("#landingImage, #imgBlkFront")
        if land and land.get("data-old-hires"):
            images.append(land["data-old-hires"])
        for i in soup.select("#altImages img"):
            src = i.get("src") or ""
            if src and "play-icon" not in src and "PKdp-play" not in src and "transparent-pixel" not in src:
                thumbs.append(src)
                images.append(full_size_image(src))

    videos = []
    for m in re.finditer(r'"videos"\s*:\s*', html):
        try:
            arr = json.JSONDecoder().raw_decode(html, m.end())[0]
        except ValueError:
            continue
        if isinstance(arr, list):
            videos += [v["url"] for v in arr
                       if isinstance(v, dict) and str(v.get("url", "")).startswith("http")]
    return {"images": dedupe(images), "thumbnails": dedupe(thumbs), "videos": dedupe(videos)}


# ── Ratings and reviews ──────────────────────────────────────────────────────

def extract_rating_summary(soup) -> dict:
    pop = soup.select_one("#acrPopover")
    overall = to_number(pop.get("title")) if pop and pop.get("title") else \
        to_number(_first_text(soup, "[data-hook='rating-out-of-text']", "#acrPopover .a-icon-alt"))
    total_ratings = to_int(_first_text(soup, "#acrCustomerReviewText", "[data-hook='total-review-count']"))
    dist: dict[str, int] = {}
    for a in soup.select("#histogramTable [aria-label], .histogram-row-container[aria-label]"):
        mm = re.search(r"(\d+)\s*percent of reviews have (\d) stars?", a.get("aria-label", ""), re.I)
        if mm:
            dist[f"{mm.group(2)}_star"] = int(mm.group(1))
    if not dist:
        for row in soup.select("#histogramTable tr, #histogramTable li"):
            t = _text(row) or ""
            star, pct = re.search(r"(\d)\s*star", t, re.I), re.search(r"(\d+)%", t)
            if star and pct:
                dist[f"{star.group(1)}_star"] = int(pct.group(1))
    return {"overall_rating": overall if overall is not None and 0 < overall <= 5 else None,
            "total_ratings": total_ratings,
            "distribution": dict(sorted(dist.items(), reverse=True))}


def breakdown_counts(percentages: dict[str, int], total_ratings: int | None) -> dict[str, int]:
    """Amazon shows the star histogram as percentages of ``total_ratings``;
    Flipkart's ``breakdown`` holds counts. Derive counts (``1_star`` .. ``5_star``)
    when the total is known, else return {} rather than guess."""
    if not percentages or not total_ratings:
        return {}
    return {k: int(round(percentages[k] * total_ratings / 100)) for k in sorted(percentages)}


def extract_aspects(soup) -> tuple[dict, str | None]:
    """Amazon's "Customers say" block: per-aspect mention counts in Flipkart's
    ``aspect_ratings`` shape, plus the AI summary text. Empty when absent."""
    aspects: dict[str, dict] = {}
    for el in soup.select("[data-hook='cr-insights-widget-aspects'] [aria-label], "
                          "#cr-dp-insights-aspects [aria-label]"):
        label = clean_text(el.get("aria-label")) or ""
        m = re.match(r"^(.+?),\s*(\d[\d,]*)\s+customers?\s+mention", label, re.I)
        if not m:
            continue
        name, mentions = m.group(1).strip(), to_int(m.group(2))
        pos = re.search(r"(\d[\d,]*)\s+positive", label, re.I)
        neg = re.search(r"(\d[\d,]*)\s+negative", label, re.I)

        def pct(hit):
            return int(round(to_int(hit.group(1)) * 100 / mentions)) if hit and mentions else None

        aspects[name] = {"rating": None, "mentions": mentions,
                         "positive_feedback_percent": pct(pos), "negative_feedback_percent": pct(neg)}
    summary = _first_text(soup, "[data-hook='cr-insights-widget-summary'] p",
                          "#product-summary p span", "#product-summary p")
    return aspects, summary


_REVIEW_TITLE_SEL = ("[data-hook='review-title']", "[data-hook='reviewTitle']",
                     ".review-title-content", ".review-title")
_REVIEW_BODY_SEL = ("[data-hook='reviewRichContentContainer']", "[data-hook='review-body']",
                    "[data-hook='review-collapsed']", "[data-hook='reviewText']",
                    ".review-text-content", ".review-text")
# Screen-reader labels and expander chrome inside review cards (not review text).
_REVIEW_JUNK = ("i, .a-icon-alt, .a-letter-space, script, style, .cr-translated-review-content, "
                "[class*='a-teaser-describedby'], .a-see-more, .a-see-more-text, .a-see-less-text, "
                "[data-hook='reviewExpandButtonContainer'], [data-hook='translationSpinner']")


def _review_part(block, selectors) -> str | None:
    """Text of the first matching element, preferring the original (untranslated) text."""
    for sel in selectors:
        el = block.select_one(sel)
        if not el:
            continue
        el = el.select_one(".cr-original-review-content") or el
        for junk in el.select(_REVIEW_JUNK):
            junk.decompose()
        t = _text(el)
        if t:
            t = re.sub(r"^\d(?:\.\d)? out of 5 stars\s*", "", t)
            t = re.sub(r"\s*(Read more|See more|Read less|The media could not be loaded\.?)\s*", " ", t)
            t = clean_text(t)
            if t:
                return t
    return None


def _review_id(block) -> str | None:
    for candidate in [block.get("id") or ""] + [e.get("id") or "" for e in block.select("[id^='customer_review']")]:
        rid = re.sub(r"^customer_review(_foreign)?-", "", candidate)
        if REVIEW_ID_RE.match(rid):
            return rid
    link = block.select_one("a[href*='/customer-reviews/']")
    m = re.search(r"/customer-reviews/(R[A-Z0-9]{8,})", link.get("href") or "") if link else None
    return m.group(1) if m else None


def extract_review_date(date_block_text: str) -> str | None:
    """Parse one review's 'Reviewed in X on <date>' block text (the exact text
    content of a '[data-hook=review-date]' node) into an ISO date string, the
    same way ``parse_reviews`` does. Used for a cheap incremental date check
    during pagination, without a full page re-parse."""
    dm = re.search(r"Reviewed in (.+?) on (.+)$", date_block_text or "")
    return iso_date(clean_text(dm.group(2))) if dm else None


def parse_reviews(soup) -> list[dict]:
    blocks = soup.select("[data-hook='review']") or \
        soup.select("[id^='customer_review-'], [id^='customer_review_foreign-']")
    reviews = []
    for r in blocks:
        rating = to_number(_first_text(
            r, "[data-hook='review-star-rating'] .a-icon-alt", "[data-hook='cmps-review-star-rating'] .a-icon-alt",
            "i.review-rating .a-icon-alt", "i[class*='a-star'] .a-icon-alt"))
        country = date = date_text = None
        dm = re.search(r"Reviewed in (.+?) on (.+)$", _text(r.select_one("[data-hook='review-date']")) or "")
        if dm:
            country, date_text = dm.group(1), clean_text(dm.group(2))
            date = iso_date(date_text)
        helpful_txt = _text(r.select_one("[data-hook='helpful-vote-statement']"))
        if helpful_txt:
            helpful = 1 if helpful_txt.lower().startswith("one") else to_int(helpful_txt)
        else:
            helpful = None
        whole = _text(r) or ""
        # Same review keys as Flipkart (reviewer_name .. helpful_count), followed
        # by the Amazon-only fields.
        reviews.append({
            "reviewer_name": _first_text(r, ".a-profile-name", "[data-hook='genome-widget'] .a-profile-content"),
            "rating": rating if rating is not None and 0 < rating <= 5 else None,
            "title": _review_part(r, _REVIEW_TITLE_SEL),
            "review_text": _review_part(r, _REVIEW_BODY_SEL),
            "verified_purchase": bool(r.select_one("[data-hook='avp-badge'], [data-hook='avp-badge-linkless']"))
            or "Verified Purchase" in whole,
            "posted_date": date,
            "posted_relative": date_text,
            "helpful_count": helpful,
            "review_id": _review_id(r),
            "reviewer_location": country,
            "country_code": country_code(country),
            "variant": _text(r.select_one("[data-hook='format-strip']")),
            "images": dedupe([full_size_image(i.get("src")) for i in
                              r.select("img[data-hook='review-image-tile'], img.review-image-tile, "
                                       "img[class*='review-image-tile']")]),
            "videos": dedupe([v.get("data-video-url") for v in r.select("[data-video-url]")]),
        })
    return reviews


_REVIEW_TOTAL_RES = (
    re.compile(r"([\d,]+)\s+with reviews", re.I),
    # "269 customer reviews", "108 matching customer reviews", "1 global review"
    re.compile(r"([\d,]+)\s+(?:matching\s+)?(?:customer|global)\s+reviews?", re.I),
)
SHOW_MORE_SEL = "[data-hook='show-more-button']"


def review_total(info: str | None) -> int | None:
    """Review count a review listing reports for its current filters."""
    for rx in _REVIEW_TOTAL_RES:
        m = rx.search(info or "")
        if m:
            return to_int(m.group(1))
    return None


def parse_review_page(html: str) -> dict:
    """Reviews currently in a review listing, the count Amazon reports for the
    listing's filters, and whether more can be loaded. Amazon's review listing
    no longer paginates by URL (``pageNumber`` is ignored): more reviews are
    appended in place by the "Show 10 more reviews" button (``has_more``); the
    old numbered pager is still recognised (``has_next``)."""
    soup = soup_of(html)
    more = soup.select_one(SHOW_MORE_SEL)
    has_more = bool(more) and not more.find_parent(class_="a-button-disabled")
    return {
        "reviews": parse_reviews(soup),
        "total_reviews": review_total(_text(soup.select_one("[data-hook='cr-filter-info-review-rating-count']"))),
        "has_more": has_more,
        "has_next": bool(soup.select_one("li.a-last:not(.a-disabled) a")) or has_more,
    }


# Words that never narrow a review search usefully.
_KEYWORD_STOP = set("""
a about above after again all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further had has have having he her here hers
him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our
out over own same she should so some such than that the their them then there these they this those through to
too under until up very was we were what when where which while who whom why will with would you your yours
product item amazon one get got also really much even well like using use used buy bought purchase purchased
good great nice best bad poor ok okay very quality price value money worth please dont didnt doesnt cant wont
isnt wasnt ive its im youre thats per yet way new day now two lot
""".split())


def review_keywords(reviews: list[dict], exclude: set[str] = frozenset(), limit: int | None = None) -> list[str]:
    """Words that occur in the given reviews' titles and text, most frequent
    first, for narrowing a review listing with Amazon's keyword filter. Each
    keyword listing has its own 100-review window, so keywords mined from the
    reviews already collected reach reviews the capped listings could not."""
    counts: dict[str, int] = {}
    for r in reviews:
        words = set(re.findall(r"[a-z]{3,}", f"{r.get('title') or ''} {r.get('review_text') or ''}".lower()))
        for w in words - _KEYWORD_STOP:
            counts[w] = counts.get(w, 0) + 1
    ranked = [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if w not in exclude]
    return ranked[:limit] if limit else ranked


def review_key(r: dict):
    """Duplicate key for a review: Amazon's own id, else a Flipkart-style
    composite of the visible fields."""
    return r.get("review_id") or (r.get("reviewer_name"), r.get("rating"), r.get("title"),
                                  (r.get("review_text") or "")[:80], r.get("posted_date"))


def merge_reviews(*lists: list[dict]) -> list[dict]:
    """The same review can appear on the product page (often truncated) and on a
    review page (complete). Keep one copy per review, filling empty fields from
    the other copy and preferring the longer text."""
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


# ── Product page: everything ─────────────────────────────────────────────────

_CAPACITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(litres?|liters?|ltrs?|L\b|tons?\b|tonnes?\b|kg\b|cu\.?\s*ft)", re.I)
# In free text (titles) "kg" is usually product weight, so only volume/tonnage
# units count there; kg capacity (e.g. washing machines) must come from a spec.
_TITLE_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(litres?|liters?|ltrs?|L\b|tons?\b)", re.I)


def parse_capacity(spec_value: str | None, title: str | None = None) -> dict | None:
    for t, rx in ((spec_value, _CAPACITY_RE), (title, _TITLE_CAPACITY_RE)):
        m = rx.search(t or "")
        if m:
            unit = m.group(2).lower().replace(".", "").replace(" ", "")
            unit = ("litre" if unit[0] == "l" else "ton" if unit.startswith("ton") else
                    "kg" if unit == "kg" else "cu_ft")
            return {"value": float(m.group(1)), "unit": unit}
    return None


def parse_energy_rating(*texts) -> str | None:
    for t in texts:
        m = re.search(r"\b(\d)\s*-?\s*Star\b", t or "", re.I)
        if m:
            return f"{m.group(1)} Star"
    return None


def parse_product_page(html: str, asin: str, base_url: str, default_currency: str) -> dict:
    """Everything the product page shows, as a Flipkart-shaped product model
    (``product_name, price{}, ratings{}, offers{}, delivery{}, seller{},
    warranty_and_policies{}, features, specifications{section: {..}},
    specifications_flat, manufacturer{}, images, videos, variants, reviews``)
    plus Amazon-only fields. Raises ValueError if this is not a product page
    (no title), so the engine can record a clean failure."""
    soup = soup_of(html)
    title = _text(soup.select_one("#productTitle"))
    if not title:
        raise ValueError("no #productTitle on page (not a product page or layout changed)")

    sections = extract_spec_sections(soup)
    specs = {k: v for rows in sections.values() for k, v in rows.items()}
    bullets = [t for t in (_text(li) for li in soup.select("#feature-bullets ul li span.a-list-item"))
               if t and not re.match(r"(see more product details|from the manufacturer)", t, re.I)]

    def _desc(t):
        if not t:
            return None
        t = clean_text(re.sub(r"\b(Previous page|Next page|From the manufacturer|Product description|Read more)\b",
                              " ", t, flags=re.I))
        return t[:5000] if t and len(t) >= 30 else None

    description = _desc(_text(soup.select_one("#productDescription"))) or \
        _desc(_text(soup.select_one("#aplus_feature_div, #aplus")))
    breadcrumbs = [t for t in (_text(a) for a in soup.select("#wayfinding-breadcrumbs_feature_div li a")) if t]

    brand = spec_get(specs, "Brand", "Brand Name")
    if not brand:
        byline = _text(soup.select_one("#bylineInfo"))
        m = re.match(r"^(?:Visit the|Brand:)\s*(.+?)(?:\s+Store)?$", byline or "", re.I)
        brand = clean_text(m.group(1)) if m else None

    offers = extract_offers(soup)
    badges = extract_badges(soup)
    availability_text = _text(soup.select_one("#availability"))
    warranty_text = spec_get(specs, "Warranty", "Warranty Description", "Manufacturer Warranty",
                             "Warranty Type") or \
        next((b for b in badges if "warranty" in b.lower()), None) or \
        next((b for b in bullets if "warranty" in b.lower()), None)

    capacity_spec = spec_get(specs, "Capacity", "Total Capacity", "Gross Capacity", "Net Capacity",
                             "Cooling Capacity (Tons)", "Tonnage")
    energy_spec = spec_get(specs, "Energy Efficiency", "Energy Rating", "Energy Star Rating",
                           "Star Rating", "BEE Star Rating")

    pricing = extract_pricing(soup, default_currency)
    seller = extract_seller(soup)
    delivery = extract_delivery(soup)
    promotion = extract_promotion(soup, offers)
    payment = payment_signals(offers, badges, soup)
    media = extract_media(soup, html)
    rating = extract_rating_summary(soup)
    aspects, customers_say = extract_aspects(soup)
    exchange_text = _text(soup.select_one("#buyBackAccordionRow, #tradeInBuyboxFeature_feature_div"))
    warranty_lines = dedupe([t for t in [warranty_text] + badges + bullets
                             if t and "warranty" in t.lower()])[:8]

    return {
        # ── identity (Flipkart: product_id / product_name / sku / brand / description)
        "product_id": asin,
        "sku": asin,
        "url": f"{base_url}/dp/{asin}",
        "product_name": title,
        "brand": brand,
        "description": description,
        "breadcrumbs": breadcrumbs,
        "features": bullets,
        # ── commerce blocks (Flipkart: price / ratings / offers / delivery / seller / policies)
        "price": {
            "currency": pricing["currency"],
            "selling_price": pricing["current_price"],
            "mrp": pricing["original_price"],
            "discount_percent": pricing["discount_percentage"],
            "discount_amount": pricing["discount_amount"],
            "special_deal_price": None,
            "extra_discount_amount": None,
            "availability": availability_text,
            "price_display": pricing["price_display"],
            "original_price_display": pricing["original_price_display"],
            "price_source": "dom" if pricing["current_price"] is not None else "none",
        },
        "ratings": {
            "average_rating": rating["overall_rating"],
            "total_ratings": rating["total_ratings"],
            "total_reviews": None,  # written-review count comes from the review pages
            "breakdown": breakdown_counts(rating["distribution"], rating["total_ratings"]),
            "breakdown_percent": rating["distribution"],
        },
        "offers": group_offers(offers, payment, promotion, exchange_text),
        "delivery": {
            "estimate": delivery["estimated_delivery"],
            "address_line": _text(soup.select_one("#glow-ingress-line2")),
            "free_delivery": delivery["delivery_cost"] == 0,
            "delivery_cost": delivery["delivery_cost"],
        },
        "seller": {
            "name": seller["seller_name"],
            "rating": None,
            "seller_id": seller["seller_id"],
            "ships_from": seller["ships_from"],
            "fulfilled_by_amazon": seller["fulfilled_by_amazon"],
        },
        "warranty_and_policies": {
            "warranty": warranty_lines,
            "return_or_replacement": [b for b in badges if re.search(r"return|replacement", b, re.I)][:2],
            "cash_on_delivery": payment["cash_on_delivery"],
            "fulfilled_by_amazon": seller["fulfilled_by_amazon"],
        },
        # ── specifications (Flipkart: sectioned + "Section | Key" flat map)
        "specifications": sections,
        "specifications_flat": flatten_specs(sections),
        "specifications_parsed_from": "table" if sections else "none",
        "manufacturer": extract_manufacturer(specs, brand),
        # ── media + variants
        "images": media["images"],
        "thumbnails": media["thumbnails"],
        "videos": media["videos"],
        "variants": extract_variants(html, base_url),
        # ── attributes Amazon exposes directly
        "model_number": spec_get(specs, "Item model number", "Model Number", "Model Name", "Model"),
        "color": spec_get(specs, "Colour", "Color"),
        "capacity": parse_capacity(capacity_spec, title),
        "energy_rating": parse_energy_rating(energy_spec, title),
        "condition": "Renewed" if re.search(r"\b(renewed|refurbished)\b", title, re.I) else None,
        "country_of_origin": spec_get(specs, "Country of Origin"),
        "warranty_text": warranty_text,
        "availability": parse_availability(availability_text),
        "promotion": promotion,
        "payment": payment,
        "badges": badges,
        "aspect_ratings": aspects,
        "customers_say": customers_say,
        # Reviews shown on the product page; the engine merges review-page reviews in.
        "reviews": parse_reviews(soup),
    }
