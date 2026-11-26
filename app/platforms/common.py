from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from app.schemas.record import CampaignBlock, MediaItem, TenantBlock

# Placeholder tenant; the identifier layer (orchestrator) assigns the real,
# provisioned tenant/project before validation and output.
UNASSIGNED = "__unassigned__"

_BLANK = {"", "n/a", "na", "null", "none", "-"}

_COUNTRY_NAME_TO_CODE = {
    "india": "IN", "china": "CN", "united states": "US", "united states of america": "US",
    "usa": "US", "us": "US", "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES", "netherlands": "NL",
    "canada": "CA", "australia": "AU", "brazil": "BR", "japan": "JP", "south korea": "KR",
    "russia": "RU", "vietnam": "VN", "indonesia": "ID", "turkey": "TR", "mexico": "MX",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "singapore": "SG",
    "sweden": "SE", "poland": "PL", "belgium": "BE", "egypt": "EG", "ireland": "IE",
}
# The rest of ISO 3166-1, plus the spellings marketplaces display (Alibaba
# shows buyers from every country: "Viet Nam", "Türkiye", "Korea", ...).
# Entries above keep precedence.
_ISO_COUNTRIES = """
AF afghanistan|AL albania|DZ algeria|AS american samoa|AD andorra|AO angola|AI anguilla|AQ antarctica
AG antigua and barbuda|AR argentina|AM armenia|AW aruba|AT austria|AZ azerbaijan|BS bahamas|BH bahrain
BD bangladesh|BB barbados|BY belarus|BZ belize|BJ benin|BM bermuda|BT bhutan|BO bolivia
BA bosnia and herzegovina|BW botswana|BN brunei|BN brunei darussalam|BG bulgaria|BF burkina faso|BI burundi
CV cabo verde|CV cape verde|KH cambodia|CM cameroon|KY cayman islands|CF central african republic|TD chad
CL chile|CO colombia|KM comoros|CG congo|CG republic of the congo|CD democratic republic of the congo
CD congo (drc)|CR costa rica|CI cote d'ivoire|CI côte d'ivoire|CI ivory coast|HR croatia|CU cuba
CW curacao|CW curaçao|CY cyprus|CZ czech republic|CZ czechia|DK denmark|DJ djibouti|DM dominica
DO dominican republic|EC ecuador|SV el salvador|GQ equatorial guinea|ER eritrea|EE estonia|SZ eswatini
SZ swaziland|ET ethiopia|FO faroe islands|FJ fiji|FI finland|GF french guiana|PF french polynesia|GA gabon
GM gambia|GE georgia|GH ghana|GI gibraltar|GR greece|GL greenland|GD grenada|GP guadeloupe|GU guam
GT guatemala|GG guernsey|GN guinea|GW guinea-bissau|GY guyana|HT haiti|HN honduras|HK hong kong
HK hong kong sar|HK hong kong, china|HU hungary|IS iceland|IR iran|IQ iraq|IM isle of man|IL israel
CI cote divoire|JM jamaica|JE jersey|JO jordan|KZ kazakhstan|KE kenya|KI kiribati|KR korea
KR republic of korea|KP north korea|XK kosovo|KW kuwait|KG kyrgyzstan|LA laos|LA lao pdr|LV latvia
LB lebanon|LS lesotho|LR liberia|LY libya|LI liechtenstein|LT lithuania|LU luxembourg|MO macau|MO macao
MO macau sar|MG madagascar|MW malawi|MY malaysia|MV maldives|ML mali|MT malta|MH marshall islands
MQ martinique|MR mauritania|MU mauritius|YT mayotte|FM micronesia|MD moldova|MC monaco|MN mongolia
ME montenegro|MS montserrat|MA morocco|MZ mozambique|MM myanmar|MM burma|NA namibia|NR nauru|NP nepal
NC new caledonia|NZ new zealand|NI nicaragua|NE niger|NG nigeria|MK north macedonia|MK macedonia
MP northern mariana islands|NO norway|OM oman|PK pakistan|PW palau|PS palestine|PA panama
PG papua new guinea|PY paraguay|PE peru|PH philippines|PT portugal|PR puerto rico|QA qatar|RE reunion
RE réunion|RO romania|RU russian federation|RW rwanda|KN saint kitts and nevis|LC saint lucia
VC saint vincent and the grenadines|WS samoa|SM san marino|ST sao tome and principe|SN senegal|RS serbia
SC seychelles|SL sierra leone|SX sint maarten|SK slovakia|SI slovenia|SB solomon islands|SO somalia
ZA south africa|SS south sudan|LK sri lanka|SD sudan|SR suriname|CH switzerland|SY syria|TW taiwan
TW taiwan, china|TJ tajikistan|TZ tanzania|TH thailand|TL timor-leste|TL east timor|TG togo|TO tonga
TT trinidad and tobago|TN tunisia|TR türkiye|TR turkiye|TM turkmenistan|TC turks and caicos islands
TV tuvalu|UG uganda|UA ukraine|UY uruguay|UZ uzbekistan|VU vanuatu|VA vatican city|VE venezuela
VN viet nam|VG british virgin islands|VI u.s. virgin islands|YE yemen|ZM zambia|ZW zimbabwe
GB england|GB scotland|GB wales|GB northern ireland|AT österreich|DE deutschland|US united states (us)
AE u.a.e.|GB u.k.|US u.s.|US u.s.a.|KR korea, republic of|IR iran, islamic republic of
"""
for _entry in re.split(r"[|\n]", _ISO_COUNTRIES):
    _entry = _entry.strip()
    if _entry:
        _code, _name = _entry.split(" ", 1)
        _COUNTRY_NAME_TO_CODE.setdefault(_name, _code)

_DATE_FORMATS = (
    "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_blank(value) -> bool:
    return value is None or str(value).strip().lower() in _BLANK


def clean_text(s) -> str | None:
    """Collapse whitespace and strip invisible direction marks Amazon et al. embed."""
    if s is None:
        return None
    s = str(s)
    for ch in ("‎", "‏", "‪", "‬", "​"):
        s = s.replace(ch, "")
    s = re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()
    return s or None


def country_code(value) -> str | None:
    """ISO-3166 alpha-2 code from a 2-letter code or a country name; else None."""
    if is_blank(value):
        return None
    s = re.sub(r"^the\s+", "", str(value).strip(), flags=re.I)
    # Names first, so non-ISO abbreviations ("UK") map to their ISO code.
    code = _COUNTRY_NAME_TO_CODE.get(s.lower())
    if code:
        return code
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return None


def parse_date(value) -> date | None:
    """Parse common English date formats. Returns None (never the raw string)
    when the value cannot be parsed."""
    if is_blank(value):
        return None
    s = re.sub(r"(?i)^\s*(posted|updated|published|reviewed)\s*(on)?[:\s]+", "", str(value).strip())
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def iso_date(value) -> str | None:
    d = parse_date(value)
    return d.isoformat() if d else None


def sha_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()


def dedupe(seq) -> list:
    """Order-preserving de-duplication that drops falsy items."""
    seen, out = set(), []
    for x in seq or []:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def media_items(images, videos) -> list[MediaItem]:
    items = [MediaItem(media_type="image", url=u) for u in dedupe(images)]
    items += [MediaItem(media_type="video", url=u) for u in dedupe(videos)]
    return items


def placeholder_tenant() -> TenantBlock:
    # Real tenant/project are assigned by the identifier layer; platform mappers
    # never provision tenant identity themselves.
    return TenantBlock(tenant_id=UNASSIGNED, project_id=UNASSIGNED)


def empty_campaign() -> CampaignBlock:
    # Campaign is not an API input; the block is emitted with null values so the
    # output schema is stable across platforms.
    return CampaignBlock()


_INDUSTRY_WORDS = (
    ("Electronics", ("mobile", "phone", "laptop", "computer", "tv", "television", "headphone", "audio",
                     "camera", "electronic", "tablet", "monitor", "wearable", "smartwatch")),
    ("Home Appliances", ("refrigerator", "washing machine", "air conditioner", "microwave", "appliance",
                         "vacuum", "water purifier")),
    ("Fashion", ("shirt", "dress", "clothing", "shoe", "footwear", "jeans", "fashion", "apparel",
                 "jewellery", "jewelry", "watch")),
    ("Beauty & Personal Care", ("beauty", "cosmetic", "perfume", "skincare", "skin care", "hair", "lipstick",
                                "makeup", "personal care", "grooming", "shampoo", "lotion", "serum")),
    ("Home & Kitchen", ("furniture", "kitchen", "home", "bed", "decor", "drinkware", "bottle", "cookware")),
)


def infer_industry(text: str | None) -> str:
    """Coarse industry from category / product words (same table the Amazon
    mapper uses, plus a few B2B words); "Consumer Goods" when nothing matches."""
    t = (text or "").lower()
    for industry, words in _INDUSTRY_WORDS:
        if any(w in t for w in words):
            return industry
    return "Consumer Goods"


def warranty_months(text) -> int | None:
    """Duration in months from text like '1 Year', '18 months', '2 yrs'."""
    if is_blank(text):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mos?)\b", str(text), re.I)
    if not m:
        return None
    value = float(m.group(1))
    return int(value * 12) if m.group(2).lower().startswith(("y",)) else int(value)


# ── requested-model relevance (match_exact_model / exclude related & accessories) ──

_GENERIC_QUERY_WORDS = {
    "the", "a", "an", "for", "with", "and", "or", "of", "in", "on", "new", "original",
    "genuine", "official", "india", "pack", "set", "combo", "edition", "model", "piece",
    "pcs", "buy", "online",
}

_ACCESSORY_WORDS = (
    "case", "cover", "pouch", "sleeve", "skin", "screenguard", "screen guard",
    "screenprotector", "screen protector", "temperedglass", "tempered glass",
    "charger", "charging cable", "cable", "adapter", "power bank", "powerbank",
    "strap", "band", "stand", "holder", "mount", "dock", "bumper", "flip cover",
    "back cover", "protector", "earphone", "earbud", "holster",
)


def _tokenize_words(text) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


def _required_model_tokens(search) -> set[str]:
    """Significant tokens from the requested product (+ brand, if given), minus
    filler words. keywords/exclude_keywords are deliberately left out: they
    broaden or narrow a search rather than identify the model itself."""
    tokens = _tokenize_words(search.product) | _tokenize_words(getattr(search, "brand", None))
    return {t for t in tokens if t not in _GENERIC_QUERY_WORDS and len(t) > 1}


def matches_requested_model(product_name, search) -> bool:
    """True unless the request names a specific model (any token carries a
    digit, e.g. "15", "a16", "5g", "256gb") and the candidate title is missing
    one of its tokens. A plain category search ("refrigerator", "water bottle")
    has no digit token and is left unfiltered - this only tightens results once
    the request is specific enough to have a "wrong model" to confuse it with."""
    required = _required_model_tokens(search)
    if not required or not any(any(ch.isdigit() for ch in t) for t in required):
        return True
    name_tokens = _tokenize_words(product_name)
    return required.issubset(name_tokens)


def is_accessory_listing(product_name, search) -> bool:
    """True when the candidate reads as an accessory (case/charger/screen
    guard/...) rather than the product itself - unless the user's own search
    text already asked for that accessory."""
    name = (product_name or "").lower()
    requested = f"{search.product or ''} {' '.join(search.keywords or [])}".lower()
    return any(word in name and word not in requested for word in _ACCESSORY_WORDS)


# ── cross-platform canonical/variant product identity ──────────────────────

_VARIANT_SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:gb|tb|mb)\b(?:\s?(?:ram|rom|storage|memory))?", re.I
)
_VARIANT_COLOR_WORDS = {
    "black", "white", "blue", "navy", "red", "green", "yellow", "purple", "pink",
    "gold", "silver", "gray", "grey", "lavender", "mint", "titanium", "graphite",
    "sierra", "cream", "orange", "bronze", "copper", "rose", "midnight",
    "starlight", "beige", "teal", "violet", "charcoal", "platinum", "champagne",
    "coral", "maroon", "olive", "turquoise", "awesome", "phantom", "dark", "light",
}


def _strip_variant_tokens(text: str) -> str:
    """Remove storage/RAM-size tokens and colour words, leaving the model-line
    name as a sorted, deduplicated token set (e.g. "Samsung Galaxy A36 5G
    256GB 8GB Dark Blue" and "Samsung Samsung Galaxy A36 5G Dark Blue 256GB"
    both -> "5g a36 galaxy samsung"). Sorting+deduping makes the result
    independent of word order and of the brand name being repeated (common
    when a product_name already embeds the brand), so the same model counts
    as the same canonical product across platforms regardless of which
    configuration/colour/phrasing was listed."""
    t = _VARIANT_SIZE_RE.sub(" ", text or "")
    words = {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in _VARIANT_COLOR_WORDS}
    return " ".join(sorted(words))


def canonical_ids(brand, product_name, capacity=None, color=None) -> tuple[str | None, str | None]:
    """(canonical_product_id, variant_key): the first groups the same model
    LINE across platforms/configurations (for "cheapest Galaxy A36 5G
    anywhere"-style rollups); the second additionally pins the specific
    storage+colour configuration, so a price comparison only treats two
    listings as comparable when they are genuinely the same SKU, not just the
    same model name with a different RAM/colour. Never used to merge records -
    purely an additional grouping key for downstream comparison."""
    base = _strip_variant_tokens(f"{brand or ''} {product_name or ''}")
    if not base:
        return None, None
    canonical_product_id = sha_hash("canonical_product_v1", base)
    cap_key = ""
    if isinstance(capacity, dict) and capacity.get("value") is not None:
        cap_key = f"{capacity.get('value')}{(capacity.get('unit') or '').strip().lower()}"
    color_key = (color or "").strip().lower()
    variant_key = sha_hash("variant_v1", canonical_product_id, cap_key, color_key)
    return canonical_product_id, variant_key


def parse_offer_amount(text, base_price=None) -> float | None:
    """Numeric discount amount from free offer text: a flat currency amount if
    present ("Up to ₹36,100 off", "Save ₹2,309"), else a percentage applied to
    base_price if one is given ("Get 5% back" -> 5% of base_price). Returns
    None when neither can be determined - never guessed from vague text like
    "discount available" with no number in it."""
    if is_blank(text):
        return None
    s = str(text)
    m = re.search(r"[₹$€£]\s*([\d,]+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    if m and base_price:
        try:
            return round(float(base_price) * float(m.group(1)) / 100.0, 2)
        except ValueError:
            pass
    return None


def best_offer_amount(texts, base_price=None) -> float | None:
    """The single largest parseable numeric offer amount across a list of
    offer strings (e.g. several bank-card offers) - never a sum, since a
    buyer can typically only use one such offer per purchase."""
    amounts = [a for a in (parse_offer_amount(t, base_price) for t in (texts or [])) if a is not None]
    return max(amounts) if amounts else None


# ── distinct per-offer-type effective prices ────────────────────────────────

def effective_prices(selling_price, currency, *, bank_offer_amount=None,
                      coupon_amount=None, exchange_amount=None) -> dict:
    """bank_offer_adjusted_price / coupon_adjusted_price / exchange_adjusted_price
    / final_effective_price as plain {"amount", "currency"} dicts (or None),
    kept distinct rather than folded into one number. Only populated when a
    real numeric offer amount is given - never estimated from a boolean/text
    flag. final_effective_price is the single lowest price reachable through
    ANY ONE no-strings-attached offer (bank or coupon, not both stacked
    together, since real stacking rules are retailer- and offer-specific and
    assuming they stack would overstate the deal). exchange_adjusted_price is
    reported separately and excluded from final_effective_price because it
    requires trading in a device, which is conditional, not a cash price."""
    out = {
        "bank_offer_adjusted_price": None,
        "coupon_adjusted_price": None,
        "exchange_adjusted_price": None,
        "final_effective_price": None,
    }
    if selling_price is None:
        return out

    def _money(amount):
        return {"amount": round(amount, 2), "currency": currency}

    candidates = []
    if bank_offer_amount:
        v = max(0.0, selling_price - bank_offer_amount)
        out["bank_offer_adjusted_price"] = _money(v)
        candidates.append(v)
    if coupon_amount:
        v = max(0.0, selling_price - coupon_amount)
        out["coupon_adjusted_price"] = _money(v)
        candidates.append(v)
    if exchange_amount:
        out["exchange_adjusted_price"] = _money(max(0.0, selling_price - exchange_amount))
    if candidates:
        out["final_effective_price"] = _money(min(candidates))
    return out


# ── title-vs-specification conflict detection ───────────────────────────────

_DISPLAY_TECH_GROUPS = (
    {"super amoled plus", "super amoled", "dynamic amoled", "amoled", "oled"},
    {"ips lcd", "tft lcd", "lcd", "tft", "ips"},
)
_REFRESH_RATE_RE = re.compile(r"\b(60|90|120|144)\s?hz\b", re.I)


def _flatten_spec_dict(d) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(_flatten_spec_dict(v))
        elif v not in (None, "", [], {}):
            out[str(k)] = v
    return out


def _tech_group(word: str) -> int | None:
    for i, group in enumerate(_DISPLAY_TECH_GROUPS):
        if word in group:
            return i
    return None


# ── independent per-variant records: stable IDs + review-to-variant matching ─

def variant_id(parent_product_id, platform_variant_id) -> str | None:
    """Deterministic internal variant id derived from the parent product id and
    the platform's own variant identifier (ASIN, item id, SKU...) - never a
    random/incrementing id, so the same variant hashes to the same id across
    runs. None when either input is missing (never fabricated)."""
    if is_blank(parent_product_id) or is_blank(platform_variant_id):
        return None
    return sha_hash("variant_id_v1", str(parent_product_id), str(platform_variant_id))


def _normalize_attrs(attrs: dict) -> dict:
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in (attrs or {}).items() if v}


def best_variant_match(attrs: dict, combinations: list[dict]) -> tuple[dict | None, float]:
    """Best-matching entry of `combinations` (each a dict with an
    "attributes" sub-dict) against a review's (or other source's) own parsed
    attrs, by counting matching key/value pairs. Returns (match_or_None,
    confidence) where confidence is matched/given (1.0 only when every given
    attribute matched and no other combination tied it - a tie is reported as
    no match, since guessing between two equally-likely variants is exactly
    the kind of unverified inference this should not do silently)."""
    given = _normalize_attrs(attrs)
    if not given or not combinations:
        return None, 0.0
    scored = []
    for combo in combinations:
        candidate = _normalize_attrs(combo.get("attributes") or {})
        matched = sum(1 for k, v in given.items() if candidate.get(k) == v)
        if matched:
            scored.append((matched, combo))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda t: t[0], reverse=True)
    top_score = scored[0][0]
    tied = [c for s, c in scored if s == top_score]
    if len(tied) != 1:
        return None, 0.0  # ambiguous - do not guess between tied candidates
    return tied[0], round(top_score / len(given), 2)


def detect_spec_conflicts(product_name, specifications) -> list[str]:
    """Field-level validation: flag when the product title and the
    specification table disagree on a well-known, checkable attribute
    (display technology, refresh rate), instead of silently accepting
    whichever value happens to be read first. Returns human-readable conflict
    strings for QualityBlock.validation_errors; never decides which value is
    "correct" - that needs a human or a source-specific rule, not a guess."""
    conflicts: list[str] = []
    title = (product_name or "").lower()
    flat_text = " ".join(f"{k} {v}" for k, v in _flatten_spec_dict(specifications).items()).lower()
    if not title or not flat_text:
        return conflicts

    title_techs = {t for group in _DISPLAY_TECH_GROUPS for t in group if t in title}
    spec_techs = {t for group in _DISPLAY_TECH_GROUPS for t in group if t in flat_text}
    title_groups = {g for g in (_tech_group(t) for t in title_techs) if g is not None}
    spec_groups = {g for g in (_tech_group(t) for t in spec_techs) if g is not None}
    if title_groups and spec_groups and not (title_groups & spec_groups):
        conflicts.append(
            f"display technology: title says {sorted(title_techs)} but specifications say {sorted(spec_techs)}"
        )

    title_hz = set(_REFRESH_RATE_RE.findall(title))
    spec_hz = set(_REFRESH_RATE_RE.findall(flat_text))
    if title_hz and spec_hz and not (title_hz & spec_hz):
        conflicts.append(
            f"display refresh rate: title says {sorted(title_hz)}Hz but specifications say {sorted(spec_hz)}Hz"
        )
    return conflicts
