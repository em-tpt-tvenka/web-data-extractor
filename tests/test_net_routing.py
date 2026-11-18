from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.core.config import settings
from app.platforms import net_routing
from app.platforms.net_routing import NetworkRoute, NetworkRouteError

_US = {
    "us_routing_enabled": True,
    "us_proxy_enabled": True,
    "us_vpn_enabled": True,
    "us_proxy_url": "http://user:pass@us-proxy.example.com:8000",
    "us_vpn_url": "socks5://user:pass@us-vpn.example.com:1080",
    "us_proxy_platforms": "walmart,bestbuy",
    "us_expected_country": "US",
    "us_route_timeout": 5,
    "us_route_max_retries": 1,
    "us_ip_validation_enabled": True,
    "proxy_url": None,
}


@pytest.fixture
def us_config():
    saved = {k: getattr(settings, k) for k in _US}
    for k, v in _US.items():
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)


class _FakeResponse:
    def __init__(self, ok, payload):
        self.ok = ok
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeRequest:
    def __init__(self, country="US", ok=True):
        self._country = country
        self._ok = ok

    async def get(self, url, timeout=None):
        if self._country is None:
            raise RuntimeError("proxy connection refused")
        return _FakeResponse(self._ok, {"ip": "203.0.113.7", "country": self._country})


class _FakeContext:
    def __init__(self, country="US", ok=True):
        self.request = _FakeRequest(country, ok)
        self.closed = False

    async def add_init_script(self, *_):
        pass

    async def add_cookies(self, *_):
        pass

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, ctx):
        self._ctx = ctx
        self.closed = False

    async def new_context(self, **_):
        return self._ctx

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, behaviors):
        # behaviors: list of ("ok"|"fail", country) applied per launch call
        self._behaviors = list(behaviors)
        self.launch_calls = []

    def _next(self):
        return self._behaviors.pop(0) if self._behaviors else ("ok", "US")

    async def launch(self, *, headless, channel, args, proxy):
        self.launch_calls.append(proxy)
        kind, country = self._next()
        if kind == "fail":
            raise RuntimeError("launch connection failed")
        return _FakeBrowser(_FakeContext(country=country))

    async def launch_persistent_context(self, profile_dir, **kwargs):
        self.launch_calls.append(kwargs.get("proxy"))
        kind, country = self._next()
        if kind == "fail":
            raise RuntimeError("launch connection failed")
        return _FakeContext(country=country)


class _FakePW:
    def __init__(self, behaviors):
        self.chromium = _FakeChromium(behaviors)


async def _open(pw, platform):
    return await net_routing.open_routed_context(
        pw, platform, headless=True, channel="chrome", profile_dir=None,
        user_agent="UA", launch_args=[], on_context=None)


def test_parse_proxy_url_forms():
    assert net_routing.parse_proxy_url(None) is None
    assert net_routing.parse_proxy_url("") is None
    assert net_routing.parse_proxy_url("us-host:8000") == {"server": "http://us-host:8000"}
    p = net_routing.parse_proxy_url("http://u:p%40x@us-host:8000")
    assert p["server"] == "http://us-host:8000"
    assert p["username"] == "u" and p["password"] == "p@x"
    s = net_routing.parse_proxy_url("socks5://us-host:1080")
    assert s["server"] == "socks5://us-host:1080"


def test_routes_for_us_platforms(us_config):
    assert net_routing.routes_for("walmart") == [NetworkRoute.US_PROXY, NetworkRoute.US_VPN]
    assert net_routing.routes_for("bestbuy") == [NetworkRoute.US_PROXY, NetworkRoute.US_VPN]


def test_routes_for_direct_platforms(us_config):
    for p in ("amazon", "alibaba", "flipkart", "samsung", "unknown"):
        assert net_routing.routes_for(p) == [NetworkRoute.DIRECT]


def test_routes_vpn_only_when_proxy_missing(us_config):
    settings.us_proxy_url = None
    assert net_routing.routes_for("walmart") == [NetworkRoute.US_VPN]


def test_routes_empty_when_nothing_configured(us_config):
    settings.us_proxy_url = None
    settings.us_vpn_url = None
    assert net_routing.routes_for("walmart") == []
    assert net_routing.requires_us_routing("walmart") is True


def test_country_validation_helpers():
    assert net_routing.country_matches({"country": "US"}, "US") is True
    assert net_routing.country_matches({"country": "IN"}, "US") is False
    assert net_routing.country_matches({}, "US") is False
    assert net_routing.country_from_payload({"countryCode": "us"}) == "US"


def test_proxy_for_route(us_config):
    assert net_routing.proxy_for_route(NetworkRoute.US_PROXY)["server"] == "http://us-proxy.example.com:8000"
    assert net_routing.proxy_for_route(NetworkRoute.US_VPN)["server"] == "socks5://us-vpn.example.com:1080"
    assert net_routing.proxy_for_route(NetworkRoute.DIRECT) is None


def test_walmart_uses_us_proxy(us_config):
    pw = _FakePW([("ok", "US")])
    ctx, browser, route = asyncio.run(_open(pw, "walmart"))
    assert route is NetworkRoute.US_PROXY
    assert pw.chromium.launch_calls[0]["server"] == "http://us-proxy.example.com:8000"


def test_bestbuy_uses_us_proxy(us_config):
    pw = _FakePW([("ok", "US")])
    _, _, route = asyncio.run(_open(pw, "bestbuy"))
    assert route is NetworkRoute.US_PROXY


def test_proxy_failure_activates_vpn_fallback(us_config):
    # US_PROXY launch fails every retry, US_VPN succeeds.
    pw = _FakePW([("fail", None), ("fail", None), ("ok", "US")])
    _, _, route = asyncio.run(_open(pw, "walmart"))
    assert route is NetworkRoute.US_VPN


def test_wrong_country_prevents_scraping_then_falls_back(us_config):
    # US_PROXY validates to a non-US country -> rejected -> VPN used.
    settings.us_route_max_retries = 0
    pw = _FakePW([("ok", "IN"), ("ok", "US")])
    _, _, route = asyncio.run(_open(pw, "walmart"))
    assert route is NetworkRoute.US_VPN


def test_both_routes_fail_returns_controlled_error(us_config):
    settings.us_route_max_retries = 0
    pw = _FakePW([("ok", "IN"), ("ok", "IN")])
    with pytest.raises(NetworkRouteError):
        asyncio.run(_open(pw, "walmart"))


def test_missing_config_raises_not_direct(us_config):
    settings.us_proxy_url = None
    settings.us_vpn_url = None
    pw = _FakePW([("ok", "US")])
    with pytest.raises(NetworkRouteError):
        asyncio.run(_open(pw, "walmart"))
    # Never silently fell back to DIRECT: no browser was launched.
    assert pw.chromium.launch_calls == []


def test_other_platform_uses_direct_no_validation(us_config):
    pw = _FakePW([("ok", "IN")])  # IN is fine for DIRECT; no US validation runs
    _, _, route = asyncio.run(_open(pw, "amazon"))
    assert route is NetworkRoute.DIRECT
    assert pw.chromium.launch_calls == [None]


def test_direct_route_when_routing_disabled(us_config):
    settings.us_routing_enabled = False
    assert net_routing.routes_for("walmart") == [NetworkRoute.DIRECT]


if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-q"]))
