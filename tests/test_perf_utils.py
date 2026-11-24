"""Shared performance utilities: request budget, run statistics, batched
language detection."""

from __future__ import annotations

import asyncio

from app.platforms.perf import RateLimiter, RunStats
from app.processing import language


def test_rate_limiter_is_shared_across_concurrent_workers():
    limiter = RateLimiter(0.05, 0.05)

    async def worker(stamps):
        for _ in range(3):
            await limiter.acquire()
            stamps.append(asyncio.get_event_loop().time())

    async def go():
        stamps: list[float] = []
        await asyncio.gather(*(worker(stamps) for _ in range(4)))  # 4 workers, 12 requests
        return sorted(stamps)

    stamps = asyncio.run(go())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert limiter.requests == 12 and min(gaps) >= 0.045  # concurrency never raises the rate


def test_run_stats_summary():
    rs = RunStats()
    a = rs.product("A")
    a.reviews_available, a.reviews_extracted, a.failed_pages = 120, 118, 1
    a.finish("done")
    b = rs.product("B")
    b.finish("skipped_completed")
    c = rs.product("C")
    c.finish("failed")
    s = rs.summary(requests_sent=40)
    assert s["products_processed"] == 2 and s["products_failed"] == 1 and s["products_skipped_completed"] == 1
    assert s["reviews_available"] == 120 and s["reviews_scraped"] == 118 and s["failed_pages"] == 1
    assert s["requests_sent"] == 40 and len(s["per_product"]) == 3 and "started" not in s["per_product"][0]


def test_batched_detection_matches_single_detection(monkeypatch):
    texts = ["The earbuds stopped charging after a week of use", "Muy buen producto, llegó rápido",
             None, "  ", "ok", "Le son est excellent et la batterie dure longtemps"] * 3
    language._CACHE.clear()
    single = [language.detect_language(t) for t in texts]
    language._CACHE.clear()
    monkeypatch.setattr(language, "PARALLEL_THRESHOLD", 10**9)  # in-process path in tests
    assert language.detect_languages(texts) == single
    assert single[2] is None and single[3] is None and single[4] is None
