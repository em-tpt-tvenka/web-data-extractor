from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0  # deterministic, also in pool workers (set on import)

_MIN_CHARS = 3

_NORMALIZE = {"zh-cn": "zh", "zh-tw": "zh"}


_CACHE: dict[str, str | None] = {}
_CACHE_MAX = 100_000
_POOL: ProcessPoolExecutor | None = None
PARALLEL_THRESHOLD = 300


def _detect_uncached(text: str) -> str | None:
    try:
        code = detect(text)
    except LangDetectException:
        return None
    return _NORMALIZE.get(code, code)


def _remember(text: str, code: str | None) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[text] = code


def detect_language(text: str | None) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS:
        return None
    if stripped in _CACHE:
        return _CACHE[stripped]
    code = _detect_uncached(stripped)
    _remember(stripped, code)
    return code


def _workers() -> int:
    try:
        from app.core.config import settings
        configured = settings.language_workers
    except Exception:
        configured = None
    if configured is not None:
        return max(0, configured)
    return max(0, min(4, (os.cpu_count() or 1) - 1))


def _pool(workers: int) -> ProcessPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(max_workers=workers)
    return _POOL


def detect_languages(texts: list[str | None]) -> list[str | None]:
    """``detect_language`` for many texts at once (same results). Batches of
    at least ``PARALLEL_THRESHOLD`` new texts run in a reused process pool;
    smaller ones, or any pool failure, fall back to in-process detection."""
    todo = []
    seen: set[str] = set()
    for t in texts:
        s = (t or "").strip()
        if len(s) >= _MIN_CHARS and s not in _CACHE and s not in seen:
            seen.add(s)
            todo.append(s)
    workers = _workers()
    if len(todo) >= PARALLEL_THRESHOLD and workers > 1:
        try:
            chunk = max(16, len(todo) // (workers * 4))
            for s, code in zip(todo, _pool(workers).map(_detect_uncached, todo, chunksize=chunk)):
                _remember(s, code)
        except Exception:
            pass  # in-process below
    return [detect_language(t) for t in texts]
