"""Platform-agnostic helpers shared by platform mappers and parsers.

Everything here is pure and side-effect free. New platforms import from this
module instead of from another platform's package, so platforms stay isolated
from each other. (The Alibaba package predates this module and keeps its own
private copies of a few of these helpers.)
"""

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
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _COUNTRY_NAME_TO_CODE.get(s.lower())


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


def warranty_months(text) -> int | None:
    """Duration in months from text like '1 Year', '18 months', '2 yrs'."""
    if is_blank(text):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mos?)\b", str(text), re.I)
    if not m:
        return None
    value = float(m.group(1))
    return int(value * 12) if m.group(2).lower().startswith(("y",)) else int(value)
