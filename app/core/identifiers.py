from __future__ import annotations

import hashlib
try:
    from uuid import uuid7  # Python 3.14+
except ImportError:  # older Pythons: same UUIDv7 from the uuid6 package
    from uuid6 import uuid7

from app.core.exceptions import PlatformNotSupportedError

PLATFORM_CODES: dict[str, str] = {
    "alibaba": "ali",
    "amazon": "amz",
    "bestbuy": "bb",
    "walmart": "wmt",
    "ebay": "eby",
    "flipkart": "flp",
    "samsung": "sms",
}

def platform_code(platform: str) -> str:
    try:
        return PLATFORM_CODES[platform]
    except KeyError:
        raise PlatformNotSupportedError(f"no platform code configured for '{platform}'")

def _uuid7() -> str:
    return str(uuid7())

def execution_id(platform: str) -> str:
    return f"{platform_code(platform)}_job_{_uuid7()}"

def record_id(platform: str) -> str:
    return f"{platform_code(platform)}_mnt_{_uuid7()}"

def tenant_id() -> str:
    return f"ten_{_uuid7()}"

def project_id() -> str:
    return f"prj_{_uuid7()}"

def summary_id() -> str:
    return f"sum_{_uuid7()}"

def report_id() -> str:
    return f"rpt_{_uuid7()}"

def insight_id() -> str:
    return f"ins_{_uuid7()}"

def artifact_id() -> str:
    return f"art_{_uuid7()}"

def content_hash(*parts: str) -> str:
    digest = hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()
    return f"sha256:{digest}"
