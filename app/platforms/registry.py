from __future__ import annotations

from app.core.exceptions import PlatformNotSupportedError
from app.platforms.alibaba.adapter import AlibabaAdapter
from app.platforms.base import PlatformAdapter

# Only platforms with a working adapter are registered, so the API never
# advertises a platform it cannot actually scrape.
_ADAPTERS: dict[str, PlatformAdapter] = {
    AlibabaAdapter.name: AlibabaAdapter(),
}


def supported_platforms() -> list[str]:
    return sorted(_ADAPTERS)


def is_supported(platform: str) -> bool:
    return platform in _ADAPTERS


def get_adapter(platform: str) -> PlatformAdapter:
    try:
        return _ADAPTERS[platform]
    except KeyError:
        raise PlatformNotSupportedError(f"platform '{platform}' is not supported")
