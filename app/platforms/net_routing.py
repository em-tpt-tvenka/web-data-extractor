from __future__ import annotations

import asyncio
import random
from enum import Enum
from urllib.parse import unquote, urlparse

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms.browser_launch import with_chrome_fallback

_log = get_logger("net.routing")


class NetworkRoute(str, Enum):
    DIRECT = "DIRECT"
    US_PROXY = "US_PROXY"
    US_VPN = "US_VPN"


class NetworkRouteError(RuntimeError):
    pass


def parse_proxy_url(url: str | None) -> dict | None:
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy: dict = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def us_platforms() -> set[str]:
    raw = (settings.us_proxy_platforms or "").replace(";", ",")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def requires_us_routing(platform: str) -> bool:
    return bool(settings.us_routing_enabled) and (platform or "").lower() in us_platforms()


def proxy_for_route(route: NetworkRoute) -> dict | None:
    if route is NetworkRoute.US_PROXY:
        return parse_proxy_url(settings.us_proxy_url)
    if route is NetworkRoute.US_VPN:
        return parse_proxy_url(settings.us_vpn_url)
    return parse_proxy_url(settings.proxy_url)


def routes_for(platform: str) -> list[NetworkRoute]:
    if not requires_us_routing(platform):
        return [NetworkRoute.DIRECT]
    routes: list[NetworkRoute] = []
    if settings.us_proxy_enabled and settings.us_proxy_url:
        routes.append(NetworkRoute.US_PROXY)
    if settings.us_vpn_enabled and settings.us_vpn_url:
        routes.append(NetworkRoute.US_VPN)
    return routes


def primary_proxy(platform: str | None = None) -> dict | None:
    routes = routes_for(platform or "")
    return proxy_for_route(routes[0]) if routes else None


def country_from_payload(payload: dict | None) -> str | None:
    if not payload:
        return None
    for key in ("country", "country_code", "countryCode"):
        val = payload.get(key)
        if val:
            return str(val).upper()
    return None


def country_matches(payload: dict | None, expected: str) -> bool:
    return country_from_payload(payload) == (expected or "").upper()


async def validate_context_country(ctx, expected_country: str, timeout_ms: int):
    try:
        resp = await ctx.request.get(settings.us_ip_check_url, timeout=timeout_ms)
        if not resp.ok:
            return False, None, None
        data = await resp.json()
    except Exception as exc:
        _log.warning("event=ip_validation_error error=%s", exc)
        return False, None, None
    ip = (data or {}).get("ip")
    country = country_from_payload(data)
    return country == (expected_country or "").upper(), ip, country


async def _safe_close(ctx, browser) -> None:
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass


async def open_routed_context(
    pw,
    platform: str,
    *,
    headless: bool,
    channel: str,
    profile_dir: str | None,
    user_agent: str,
    launch_args: list[str],
    locale: str = "en-US",
    viewport: dict | None = None,
    on_context=None,
):
    viewport = viewport or {"width": 1440, "height": 900}
    platform_tag = (platform or "").upper()
    routes = routes_for(platform)

    if requires_us_routing(platform) and not routes:
        raise NetworkRouteError(
            f"{platform}: US routing required but no US_PROXY/US_VPN endpoint is "
            f"configured (set us_proxy_url and/or us_vpn_url)"
        )

    max_retries = max(0, int(settings.us_route_max_retries))
    last_error: Exception | None = None

    for route in routes:
        proxy = proxy_for_route(route)
        is_us = route in (NetworkRoute.US_PROXY, NetworkRoute.US_VPN)
        _log.info("[%s] Selected route: %s", platform_tag, route.value)
        if route is NetworkRoute.US_VPN:
            _log.info("[%s] Activating US VPN fallback", platform_tag)

        for attempt in range(1, max_retries + 2):
            ctx = None
            browser = None
            try:
                async def _do_launch():
                    nonlocal browser
                    if profile_dir:
                        return await pw.chromium.launch_persistent_context(
                            profile_dir, channel=channel, headless=headless, args=launch_args,
                            user_agent=user_agent, locale=locale, viewport=viewport, proxy=proxy,
                        )
                    browser = await pw.chromium.launch(
                        headless=headless, channel=channel, args=launch_args, proxy=proxy)
                    return await browser.new_context(
                        user_agent=user_agent, locale=locale, viewport=viewport)

                ctx = await with_chrome_fallback(
                    _do_launch, platform=platform, pw=pw, profile_dir=profile_dir,
                    headless=headless, user_agent=user_agent, locale=locale, proxy=proxy,
                    viewport=viewport, extra_args=launch_args,
                )

                if on_context is not None:
                    await on_context(ctx)

                if is_us and settings.us_ip_validation_enabled:
                    ok, ip, country = await validate_context_country(
                        ctx, settings.us_expected_country, settings.us_route_timeout * 1000)
                    if not ok:
                        _log.warning(
                            "[%s] %s IP validation failed (ip=%s country=%s expected=%s)",
                            platform_tag, route.value, ip, country, settings.us_expected_country)
                        await _safe_close(ctx, browser)
                        last_error = NetworkRouteError(f"{route.value} IP validation failed")
                        break
                    _log.info("[%s] US IP validation successful (ip=%s country=%s)",
                              platform_tag, ip, country)
                elif route is NetworkRoute.DIRECT:
                    _log.info("[%s] Using DIRECT route (local connection)", platform_tag)

                return ctx, browser, route
            except Exception as exc:
                last_error = exc
                _log.warning("[%s] route=%s attempt=%d/%d connection failed: %s",
                             platform_tag, route.value, attempt, max_retries + 1, exc)
                await _safe_close(ctx, browser)
                if attempt <= max_retries:
                    await asyncio.sleep(min(10.0, (2 ** (attempt - 1)) + random.uniform(0, 0.5)))

        if route is NetworkRoute.US_PROXY:
            _log.info("[%s] US proxy connection failed", platform_tag)

    raise NetworkRouteError(f"{platform}: all network routes failed ({last_error})")
