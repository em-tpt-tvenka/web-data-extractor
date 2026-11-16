from __future__ import annotations

from app.core.exceptions import PlatformNotSupportedError
from app.platforms.alibaba.adapter import AlibabaAdapter
from app.platforms.amazon.adapter import AmazonAdapter
from app.platforms.base import PlatformAdapter
from app.platforms.bestbuy.adapter import BestBuyAdapter
from app.platforms.flipkart.adapter import FlipkartAdapter
from app.platforms.samsung.adapter import SamsungAdapter
from app.platforms.walmart.adapter import WalmartAdapter

_ADAPTERS: dict[str, PlatformAdapter] = {
    adapter.name: adapter
    for adapter in (
        AlibabaAdapter(),
        AmazonAdapter(),
        FlipkartAdapter(),
        SamsungAdapter(),
        BestBuyAdapter(),
        WalmartAdapter(),
    )
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
