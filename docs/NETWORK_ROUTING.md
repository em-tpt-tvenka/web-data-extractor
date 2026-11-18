# US Network Routing (Walmart & Best Buy)

Walmart and Best Buy are US-only storefronts and are geo-blocked from a non-US
network. This document describes how the scraper routes those two platforms
through a US exit while every other platform keeps using the normal local
(Indian) connection, with automatic failover and without touching the Windows
machine's global network configuration.

## Architecture

A single module, `app/platforms/net_routing.py`, owns all routing decisions.
Nothing about routing is hardcoded inside individual scrapers.

Three routes:

| Route | Meaning | Resolves to |
|-------|---------|-------------|
| `DIRECT` | Local connection | `proxy_url` (normally none) |
| `US_PROXY` | Configured US proxy | `us_proxy_url` |
| `US_VPN` | Dedicated US VPN endpoint (fallback) | `us_vpn_url` |

Per-platform rules (configurable via `us_proxy_platforms`):

| Platform | Primary | Fallback |
|----------|---------|----------|
| Walmart | `US_PROXY` | `US_VPN` |
| Best Buy | `US_PROXY` | `US_VPN` |
| Amazon, Alibaba, Flipkart, Samsung, others | `DIRECT` | none |

### Modules and responsibilities

- `app/platforms/net_routing.py`
  - `routes_for(platform)` — ordered candidate routes for a platform.
  - `requires_us_routing(platform)` — whether the platform must use a US exit.
  - `proxy_for_route(route)` / `parse_proxy_url(url)` — resolve a route to a
    Playwright proxy dict (HTTP/HTTPS/SOCKS5, with optional user:pass).
  - `primary_proxy(platform)` — first route's proxy (back-compat for callers).
  - `validate_context_country(ctx, expected, timeout)` — public-IP + country
    check performed *through the browser's own proxy*, so it validates the real
    egress, not just that a proxy string parsed.
  - `open_routed_context(pw, platform, ...)` — the failover driver: launches the
    browser on each candidate route in order, validates it, and returns the
    first working `(context, browser, route)`; raises `NetworkRouteError` if a
    US platform has no working route (it never silently falls back to `DIRECT`).
- `app/platforms/browser.py` — `proxy_settings(platform)` now delegates to
  `net_routing.primary_proxy`; `random_user_agent` unchanged.
- `app/platforms/walmart/engine.py`, `app/platforms/bestbuy/engine.py` — call
  `open_routed_context` at launch instead of launching Chromium directly.
  Extraction, parsing, dedup and storage are unchanged.

## Failover flow

1. Identify the platform and look up its routes.
2. Walmart/Best Buy → try `US_PROXY` first.
3. Launch Chromium with that route's proxy; validate the public IP is in the US.
4. On a genuine connection failure (retried with exponential backoff up to
   `us_route_max_retries`) or a failed/again non-US validation → try `US_VPN`.
5. Validate the VPN route the same way; scrape through the first route that
   validates.
6. If both fail → raise `NetworkRouteError` (the job fails with a clear message).
7. All other platforms always use `DIRECT`.

### Website restrictions are not network failures

HTTP 403/429, CAPTCHA and verification walls are detected separately inside each
engine (`_BLOCK_RE`) and cause the scrape to stop/skip cleanly — they do **not**
trigger route failover and are **not** treated as connection errors. The scraper
respects these restrictions rather than trying to circumvent them.

## Configuration

Set these in `.env` (see `.env.example` for the full list). Never commit real
values.

```
us_routing_enabled=true
us_proxy_enabled=true
us_proxy_url=http://USER:PASS@us-host:port        # primary
us_vpn_enabled=true
us_vpn_mode=proxy_endpoint
us_vpn_url=socks5://USER:PASS@us-vpn-host:port     # fallback
us_proxy_platforms=walmart,bestbuy
us_expected_country=US
us_route_timeout=30
us_route_max_retries=2
us_ip_validation_enabled=true
us_ip_check_url=https://ipinfo.io/json
```

- **US proxy** — any US residential/datacenter proxy that speaks HTTP, HTTPS or
  SOCKS5. `host:port`, `scheme://host:port`, or `scheme://user:pass@host:port`.
- **US VPN fallback** — a *VPN-provided proxy/SOCKS endpoint*. This is the safe,
  application-level way to use a VPN for only these two platforms.

## Required VPN provider capability / infrastructure limitations

A true per-application split-tunnel OS VPN (so only Walmart/Best Buy traffic goes
through the tunnel while everything else stays on the Indian line) is **operating
-system infrastructure**, not something this process sets up:

- Windows cannot give a single Python process its own isolated VPN route; dialing
  a system VPN would reroute the whole machine, which this project must not do.
- The supported, isolated approach is a VPN that exposes a **proxy/SOCKS
  endpoint** (most commercial VPNs and self-hosted WireGuard/OpenVPN-in-a-
  container or WSL setups can). Point `us_vpn_url` at that endpoint.
- If your VPN provider cannot expose a proxy endpoint and can only toggle a
  global tunnel, the fallback is intentionally left unconfigured — run it on a
  US host / container instead of switching the machine's global network.

## Running the tests

```
python -m pytest tests/test_net_routing.py -q
```

The suite is fully mocked — it needs no real proxy or VPN and makes no network
calls. It covers route selection, proxy parsing, IP/country validation,
proxy→VPN failover, controlled errors when nothing is configured, and that other
platforms stay on `DIRECT`.

## Troubleshooting

- **Job fails: "US routing required but no US_PROXY/US_VPN endpoint is
  configured"** — set `us_proxy_url` and/or `us_vpn_url` in `.env`, then restart
  the Celery workers (they read `.env` at startup).
- **"IP validation failed (country=IN)"** — the proxy/VPN is not actually exiting
  in the US; check the endpoint.
- **"all network routes failed"** — both endpoints are unreachable or non-US.
- **Changes to `.env` not taking effect** — restart the workers; Celery does not
  hot-reload.

## Security

- Credentials live only in `.env` (git-ignored). `.env.example` holds
  placeholders. Proxy/VPN URLs and passwords are never logged.
- Validation uses a public IP-echo service and sends no user or project data.
- The global Windows network configuration and other applications are never
  modified.
