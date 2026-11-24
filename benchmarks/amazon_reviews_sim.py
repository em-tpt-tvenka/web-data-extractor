

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import subprocess

from benchmarks import procstats

from app.platforms.amazon.engine import LISTING_CAP, AmazonEngine, AmazonEngineConfig
from app.platforms.amazon.options import resolve_marketplace

SCENARIOS = {  # name: (products, reviews per product)
    "small": (10, 100), "medium": (50, 500), "large": (100, 1000), "very_large": (100, 5000),
}
# Fitted on 138 live review listings (B0GV1L119H + B0D78Y3KRP runs): a listing costs

PAGE_LOAD_S, SHOW_MORE_S, PRODUCT_PAGE_S = 4.27, 2.52, 15.0
LISTING_FAILURE_RATE = 0.01  # transient listing failures injected (retries exhausted)
_STARS = {"five_star": 5, "four_star": 4, "three_star": 3, "two_star": 2, "one_star": 1}
_STAR_MIX = (5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 4, 4, 4, 4, 3, 3, 2, 1, 1)
_VOCAB = [f"w{i}" for i in range(3000)]
_ZIPF = [1 / (i + 1) ** 1.05 for i in range(len(_VOCAB))]


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """Event loop whose clock jumps to the next timer instead of sleeping."""

    def __init__(self):
        super().__init__()
        self._vt = 0.0
        real_select = self._selector.select

        def select(timeout=None):
            if timeout and timeout > 0:
                self._vt += timeout
            return real_select(0)

        self._selector.select = select

    def time(self) -> float:
        return self._vt


def corpus(seed: int, n: int) -> list[dict]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        words = rnd.choices(_VOCAB, weights=_ZIPF, k=rnd.randint(6, 40))
        out.append({
            "review_id": f"R{seed:04d}{i:08d}", "reviewer_name": f"u{i}", "rating": float(rnd.choice(_STAR_MIX)),
            "title": " ".join(words[:3]), "review_text": " ".join(words), "verified_purchase": rnd.random() < 0.85,
            "posted_date": f"20{20 + i % 7}-{1 + i % 12:02d}-{1 + i % 28:02d}", "posted_relative": None,
            "helpful_count": rnd.randint(0, 40) or None, "reviewer_location": "India", "country_code": "IN",
            "variant": None, "images": [], "videos": [],
            "_media": rnd.random() < 0.2, "_fmt": rnd.random() < 0.4, "_h": rnd.random(), "_words": set(words),
        })
    return out


class SimAmazon:
    def __init__(self, engine: AmazonEngine, honour_have: bool, rnd: random.Random):
        self.engine, self.honour_have, self.rnd = engine, honour_have, rnd
        self.products: dict[str, list[dict]] = {}
        self.failed_requests = 0

    async def listing(self, asin, filters, have=None) -> dict:
        eng = self.engine
        await eng._throttle()  # same budget as AmazonEngine._goto (page gap + per-minute cap)
        await asyncio.sleep(PAGE_LOAD_S)
        if self.rnd.random() < LISTING_FAILURE_RATE:
            self.failed_requests += 1
            raise RuntimeError("simulated page load failure after retries")
        rows = self.products[asin]
        if filters.get("reviewerType") == "avp_only_reviews":
            rows = [r for r in rows if r["verified_purchase"]]
        if filters.get("filterByStar") in _STARS:
            rows = [r for r in rows if r["rating"] == _STARS[filters["filterByStar"]]]
        if filters.get("mediaType") == "media_reviews_only":
            rows = [r for r in rows if r["_media"]]
        if filters.get("formatType") == "current_format":
            rows = [r for r in rows if r["_fmt"]]
        if filters.get("filterByKeyword"):
            rows = [r for r in rows if filters["filterByKeyword"] in r["_words"]]
        key = (lambda r: r["posted_date"]) if filters.get("sortBy") == "recent" else (lambda r: r["_h"])
        shown = sorted(rows, key=key, reverse=True)[:LISTING_CAP]
        visible = [{k: v for k, v in r.items() if not k.startswith("_")} for r in shown]
        if self.honour_have and have is not None and len(rows) <= have():
            return {"reviews": visible[:10], "total": len(rows), "batches": 1, "stop_reason": "already collected",
                    "login_required": False, "error": None}
        pages = max(1, math.ceil(len(shown) / 10))
        for _ in range(pages - 1):  # "Show 10 more reviews", same budget as AmazonEngine._show_more
            await eng._click.acquire()
            await eng._budget.acquire()
            await asyncio.sleep(SHOW_MORE_S)
        return {"reviews": visible, "total": len(rows), "batches": pages, "stop_reason": "listing exhausted",
                "login_required": False, "error": None}


def _variant(name: str) -> dict:
    if name == "sequential":  # implementation before this optimisation
        return {"detail_concurrency": 1, "keyword_yield_window": 0, "honour_have": False}
    return {"detail_concurrency": int(name.split("_c")[1]) if "_c" in name else 2,
            "keyword_yield_window": 8, "honour_have": True}


async def _run(products: int, per_product: int, variant: str) -> dict:
    v = _variant(variant)
    cfg = AmazonEngineConfig(marketplace=resolve_marketplace("amazon.in"), search_text="x",
                             detail_concurrency=v["detail_concurrency"],
                             keyword_yield_window=v["keyword_yield_window"], keyword_min_yield=3)
    eng = AmazonEngine(cfg)
    sim = SimAmazon(eng, v["honour_have"], random.Random(99))

    async def load_listing(page, asin, filters, label, have=None):
        return await sim.listing(asin, filters, have)

    eng._load_listing = load_listing
    sem = asyncio.Semaphore(cfg.detail_concurrency)
    totals = {"available": 0, "extracted": 0, "unique": 0, "missing": 0, "duplicates": 0}

    async def product(i: int):
        asin = f"B0SIM{i:05d}"
        async with sem:
            sim.products[asin] = corpus(i, per_product)
            ps = eng.stats.product(asin)
            await eng._throttle()  # product page, as AmazonEngine._scrape_product
            await asyncio.sleep(PRODUCT_PAGE_S)
            data = {"product_id": asin, "reviews": [], "ratings": {"total_ratings": per_product, "total_reviews": None,
                    "average_rating": 4.2, "breakdown": {}, "breakdown_percent": {}}}
            summary, reviews = await eng._scrape_reviews(None, data)
            ids = [r["review_id"] for r in reviews]
            truth = {r["review_id"] for r in sim.products.pop(asin)}
            totals["available"] += len(truth)
            totals["extracted"] += len(ids)
            totals["unique"] += len(set(ids))
            totals["duplicates"] += len(ids) - len(set(ids))
            totals["missing"] += len(truth - set(ids))
            ps.reviews_available, ps.reviews_extracted = summary["reviews_available"], len(ids)
            ps.pages_processed, ps.listings_processed = summary["pages_scraped"], summary["listings_scraped"]
            ps.failed_pages = len(summary["failed_listings"])
            ps.finish("done")
            ps.seconds = None  # wall seconds are meaningless on the virtual clock

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await asyncio.gather(*[product(i) for i in range(products)])
    sim_seconds = loop.time() - t0
    run = eng.stats.summary(eng.requests_sent)
    return {
        "variant": variant, "product_concurrency": cfg.detail_concurrency,
        "simulated_seconds": round(sim_seconds), "simulated_hours": round(sim_seconds / 3600, 2),
        "reviews_available": totals["available"], "reviews_unique": totals["unique"],
        "coverage_percent": round(totals["unique"] / totals["available"] * 100, 2),
        "missing_reviews": totals["missing"], "duplicate_reviews": totals["duplicates"],
        "reviews_per_minute": round(totals["unique"] / (sim_seconds / 60), 1),
        "avg_seconds_per_product": round(sim_seconds / products, 1),
        "requests": eng.requests_sent, "page_loads": eng._nav.requests, "show_more_requests": eng._click.requests,
        "failed_requests": sim.failed_requests, "failed_listings": run["failed_pages"],
        "listings": sum(p["listings_processed"] for p in run["per_product"]),
    }


def bench(scenario: str, variant: str) -> dict:
    products, per = SCENARIOS[scenario]
    loop = VirtualClockLoop()
    asyncio.set_event_loop(loop)
    cpu0, wall0 = procstats.cpu_seconds(), procstats.wall()
    try:
        res = loop.run_until_complete(_run(products, per, variant))
    finally:
        loop.close()
    res.update(scenario=scenario, products=products, reviews_per_product=per,
               cpu_seconds=round(procstats.cpu_seconds() - cpu0, 2),
               peak_memory_mb=procstats.peak_memory_mb(),
               benchmark_wall_seconds=round(procstats.wall() - wall0, 1))
    return res


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    if sys.argv[1:2] == ["--one"]:  # one measurement per process: clean peak memory / CPU
        print(json.dumps(bench(sys.argv[2], sys.argv[3])), flush=True)
        sys.exit(0)
    for sc in sys.argv[1:] or list(SCENARIOS):
        for variant in ("sequential", "optimized_c2", "optimized_c3"):
            subprocess.run([sys.executable, "-m", "benchmarks.amazon_reviews_sim", "--one", sc, variant], check=True)
