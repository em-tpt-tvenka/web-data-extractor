"""Chromium browser launch with an automatic fallback to a directly-spawned
Google Chrome process.

Every platform engine launches its browser through Playwright's own Chromium
launcher (``pw.chromium.launch_persistent_context`` / ``pw.chromium.launch``),
using the ``chrome`` channel so it already drives the user's installed Google
Chrome rather than Playwright's bundled Chromium. That remains the preferred,
default path and is completely unchanged by this module.

Some environments block or break Playwright's own launch mechanism itself
(sandbox/policy restrictions, a missing/corrupted Playwright browser install,
a locked-down shell that refuses the exact command line Playwright builds,
etc.) while the browser is otherwise perfectly installed and usable. For
exactly that situation, :func:`with_chrome_fallback` wraps a platform's normal
launch attempt and, only if it raises, falls back to starting the OS-installed
Chrome binary directly via ``subprocess`` (bypassing Playwright's launcher
entirely) and then attaches Playwright to that running process over the
Chrome DevTools Protocol (CDP). From that point on the returned browser
context is an ordinary Playwright ``BrowserContext`` and every existing
scraping code path (parsing, review collection, etc.) works unchanged.
"""

from __future__ import annotations

import asyncio
import os
import platform as _platform
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from app.core.logging import get_logger

_log = get_logger("browser_launch")

# Chrome CLI flags that are meaningless / invalid once Chrome is already
# running (they only apply at process start-up) are passed through as-is;
# this list is just the launch args platforms already use for Playwright and
# is not filtered, since Chrome ignores unknown/duplicate flags harmlessly.


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome_executable() -> str | None:
    """Locate the installed Google Chrome / Chromium binary on this machine.

    Checked in order: an explicit ``CHROME_PATH`` env var, a PATH lookup, then
    common per-OS install locations (including the Windows registry App Paths
    entry) -- so this also finds Chrome on systems where it is installed but
    not reachable by name from a shell (not "launchable through the command
    line" in the way Playwright's own launcher expects)."""
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).is_file():
        return env

    for name in ("google-chrome-stable", "google-chrome", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found

    system = _platform.system()
    candidates: list[str] = []
    if system == "Windows":
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if root:
                candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        try:
            import winreg  # noqa: PLC0415 -- Windows-only
            key_path = r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, key_path) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        if val:
                            candidates.append(val)
                except OSError:
                    continue
        except ImportError:
            pass
    elif system == "Darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:  # Linux and other Unix-likes
        candidates += [
            "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser", "/usr/bin/chromium", "/snap/bin/chromium",
        ]

    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


async def _wait_cdp_ready(port: int, timeout: float = 20.0) -> None:
    import httpx  # noqa: PLC0415 -- optional/lazy, only needed on this fallback path

    deadline = asyncio.get_event_loop().time() + timeout
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=1.5) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    return
            except Exception as exc:
                last_exc = exc
            await asyncio.sleep(0.3)
    raise RuntimeError(f"Chrome did not become CDP-ready on port {port} within {timeout}s: {last_exc}")


async def launch_direct_chrome(
    pw,
    *,
    profile_dir: str | None,
    headless: bool,
    user_agent: str | None = None,
    locale: str | None = None,
    proxy: dict | None = None,
    extra_args: list[str] | None = None,
    viewport: dict | None = None,
):
    """Spawn the OS-installed Chrome directly (bypassing Playwright's own
    browser-launch mechanism entirely) and attach Playwright to it over CDP.

    Returns a Playwright ``BrowserContext`` whose ``.close()`` also terminates
    the spawned Chrome process, so every existing ``await ctx.close()`` call
    site needs no change to clean up correctly."""
    chrome_path = find_chrome_executable()
    if not chrome_path:
        raise RuntimeError(
            "No installed Google Chrome / Chromium executable could be found on this "
            "system (checked PATH and common install locations). Set the CHROME_PATH "
            "environment variable to its full path to enable this fallback."
        )
    port = _free_port()
    user_data_dir = profile_dir or str(Path(tempfile.gettempdir()) / f"wde-chrome-fallback-{port}")
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
    if user_agent:
        args.append(f"--user-agent={user_agent}")
    if locale:
        args.append(f"--lang={locale}")
    if proxy and proxy.get("server"):
        args.append(f"--proxy-server={proxy['server']}")
        if proxy.get("username") or proxy.get("password"):
            _log.warning("event=direct_chrome_proxy_auth_unsupported message=proxy credentials "
                         "cannot be applied to a directly-launched Chrome; only the proxy server "
                         "route is used, not username/password authentication")
    if extra_args:
        args.extend(extra_args)

    _log.warning("event=direct_chrome_launch path=%s port=%d profile=%s headless=%s",
                 chrome_path, port, user_data_dir, headless)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await _wait_cdp_ready(port)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception:
        proc.terminate()
        raise

    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    if viewport:
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.set_viewport_size(viewport)
        except Exception as exc:
            _log.warning("event=direct_chrome_viewport_failed error=%s", exc)

    original_close = ctx.close

    async def _close_and_terminate(*a, **kw):
        try:
            await original_close(*a, **kw)
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    ctx.close = _close_and_terminate  # type: ignore[method-assign]
    return ctx


async def with_chrome_fallback(
    launch_fn,
    *,
    platform: str,
    pw,
    profile_dir: str | None,
    headless: bool,
    user_agent: str | None = None,
    locale: str | None = None,
    proxy: dict | None = None,
    viewport: dict | None = None,
    extra_args: list[str] | None = None,
):
    """Call ``launch_fn()`` -- a platform's own normal Playwright launch,
    completely unchanged. If Playwright itself cannot launch/attach a browser
    (blocked, missing browser binaries, sandbox/policy restrictions, or any
    other launch-time failure -- not a page-load or scraping error, which
    ``launch_fn`` never raises), automatically fall back to starting the
    installed Google Chrome directly and attaching over CDP, so scraping can
    continue either way. ``launch_fn`` must be a zero-argument coroutine
    function returning a ``BrowserContext``."""
    try:
        return await launch_fn()
    except Exception as exc:
        _log.warning(
            "event=playwright_launch_failed platform=%s error=%s "
            "message=falling back to a directly-launched Chrome over CDP",
            platform, exc,
        )
        return await launch_direct_chrome(
            pw, profile_dir=profile_dir, headless=headless, user_agent=user_agent,
            locale=locale, proxy=proxy, extra_args=extra_args, viewport=viewport,
        )
