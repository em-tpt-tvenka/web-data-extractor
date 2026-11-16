"""Centralized identifier generation.

All generated ids use UUIDv7 and the naming convention:

    <platform_code>_<entity>_<uuid7>   e.g. ali_mnt_019a..., ali_job_019a...
    ten_<uuid7>                         tenant (platform-independent)
    prj_<uuid7>                         project (platform-independent)

Platform codes are defined once in PLATFORM_CODES and must not be hardcoded in
individual scrapers. Record ids never contain product ids, SKUs, names or URLs.
"""

from __future__ import annotations

import hashlib
from uuid import uuid7

from app.core.exceptions import PlatformNotSupportedError

PLATFORM_CODES: dict[str, str] = {
    "alibaba": "ali",
    "amazon": "amz",
    "bestbuy": "bb",
    "walmart": "wmt",
    "ebay": "eby",
    "flipkart": "flp",
}


def platform_code(platform: str) -> str:
    try:
        return PLATFORM_CODES[platform]
    except KeyError:
        raise PlatformNotSupportedError(f"no platform code configured for '{platform}'")


def _uuid7() -> str:
    return str(uuid7())


def execution_id(platform: str) -> str:
    """One id per scraping execution, reused across all its records."""
    return f"{platform_code(platform)}_job_{_uuid7()}"


def record_id(platform: str) -> str:
    """Unique id for a single review/mention/feedback record."""
    return f"{platform_code(platform)}_mnt_{_uuid7()}"


def tenant_id() -> str:
    return f"ten_{_uuid7()}"


def project_id() -> str:
    return f"prj_{_uuid7()}"


def content_hash(*parts: str) -> str:
    """SHA-256 content fingerprint for duplicate detection (not a record id)."""
    digest = hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()
    return f"sha256:{digest}"
