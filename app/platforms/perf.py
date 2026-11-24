

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass, field


class RateLimiter:
    """Randomized minimum interval between ``acquire()`` calls, shared across
    tasks. ``requests`` counts every acquisition (= requests sent)."""

    def __init__(self, min_interval: float, max_interval: float | None = None):
        self.min_interval = max(0.0, min_interval)
        self.max_interval = max(self.min_interval, max_interval if max_interval is not None else min_interval)
        self._base = (self.min_interval, self.max_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.requests = 0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            gap = random.uniform(self.min_interval, self.max_interval)
            wait = gap - (loop.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()
            self.requests += 1

    def slow_down(self, factor: float, cap: float) -> None:
        """Widen the gap (e.g. after the platform signals throttling), up to
        ``cap`` times the configured gap; stays slowed for the rest of the run."""
        lo, hi = self._base
        self.min_interval = min(self.min_interval * factor, lo * cap) if lo else self.min_interval
        self.max_interval = max(self.min_interval, min(self.max_interval * factor, hi * cap))


@dataclass
class ProductStats:
    product_id: str
    reviews_available: int | None = None
    pages_total: int | None = None  # known up front where the platform reports it
    pages_processed: int = 0
    listings_processed: int = 0
    reviews_extracted: int = 0
    unique_reviews_stored: int | None = None
    failed_pages: int = 0
    requests: int = 0
    status: str = "running"  # running | done | failed | skipped_completed
    started: float = field(default_factory=time.monotonic)
    seconds: float | None = None

    def finish(self, status: str = "done") -> None:
        self.status = status
        self.seconds = round(time.monotonic() - self.started, 2)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("started")
        return d


class RunStats:
    """Collects ``ProductStats`` and derives the run summary."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.products: dict[str, ProductStats] = {}

    def product(self, product_id: str) -> ProductStats:
        if product_id not in self.products:
            self.products[product_id] = ProductStats(product_id)
        return self.products[product_id]

    def summary(self, requests_sent: int | None = None) -> dict:
        elapsed = time.monotonic() - self.started
        ps = list(self.products.values())
        finished = [p for p in ps if p.seconds is not None and p.status != "skipped_completed"]
        scraped = sum(p.reviews_extracted for p in ps)
        stored = [p.unique_reviews_stored for p in ps if p.unique_reviews_stored is not None]
        return {
            "products_processed": len(finished),
            "products_failed": sum(p.status == "failed" for p in ps),
            "products_skipped_completed": sum(p.status == "skipped_completed" for p in ps),
            "reviews_available": sum(p.reviews_available or 0 for p in ps),
            "reviews_scraped": scraped,
            "unique_reviews_stored": sum(stored) if stored else None,
            "failed_pages": sum(p.failed_pages for p in ps),
            "requests_sent": requests_sent,
            "execution_seconds": round(elapsed, 2),
            "avg_seconds_per_product": round(sum(p.seconds for p in finished) / len(finished), 2)
            if finished else None,
            "reviews_per_minute": round(scraped / (elapsed / 60), 1) if elapsed > 0 else None,
            "per_product": [p.as_dict() for p in ps],
        }
