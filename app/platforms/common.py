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
