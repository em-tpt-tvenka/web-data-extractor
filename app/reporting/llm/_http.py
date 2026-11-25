"""Shared HTTP + JSON-extraction helpers for network LLM providers.

``httpx`` is imported lazily so the reporting package imports cleanly in
environments where no network provider is used.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.core.logging import get_logger
from app.reporting.llm.base import LLMError, LLMUnavailable

_log = get_logger("reporting.llm.http")

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _httpx():
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("httpx is not installed; pip install httpx") from exc
    return httpx


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text`` (tolerates code fences/prose)."""
    if not text:
        raise LLMError("empty LLM response")
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJ_RE.search(text)
    if not match:
        raise LLMError("no JSON object found in LLM response")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON from LLM: {exc}") from exc


async def post_json(
    url: str,
    *,
    payload: dict,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: int,
    retries: int,
) -> dict:
    """POST JSON with bounded retries + exponential backoff. Raises LLMError."""
    httpx = _httpx()
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                # Client error (bad key, bad model) — do not retry.
                raise LLMUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except LLMUnavailable:
            raise
        except Exception as exc:  # network / 5xx / 429
            last = exc
            _log.warning("event=llm_retry attempt=%d/%d error=%s", attempt, retries, exc)
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 10))
    raise LLMError(f"LLM request failed after {retries} attempts: {last}")
