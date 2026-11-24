

from __future__ import annotations

import asyncio
import json
import random
import sys
import subprocess
import time

from benchmarks import procstats

from app.core.config import settings

_WORDS = ("battery sound bass charging fit connection mic noise case volume calls comfort durable gaming "
          "music range latency touch button app quality price delivery packaging earbuds clear loud "
          "stopped working week month after using daily gym running travel phone laptop pairing bluetooth "
          "left right side issue problem worth money value recommend disappointed happy satisfied product "
          "build plastic cheap premium design colour light weight small ears pain hours backup fast slow").split()
SCENARIOS = {"small": (10, 100), "medium": (50, 500), "large": (100, 1000), "very_large": (100, 5000)}


def model(i: int, n: int) -> dict:
    rnd = random.Random(i)
    asin = f"B0BENCH{i:03d}"
    reviews = []
    for j in range(n):
        words = rnd.choices(_WORDS, k=rnd.randint(8, 40))
        reviews.append({
            "review_id": f"R{i:03d}{j:09d}", "reviewer_name": f"user {j}", "rating": float(rnd.randint(1, 5)),
            "title": " ".join(words[:4]).capitalize(), "review_text": " ".join(words).capitalize() + ".",
            "verified_purchase": rnd.random() < 0.85, "posted_date": f"2026-{1 + j % 11:02d}-{1 + j % 28:02d}",
            "posted_relative": None, "helpful_count": rnd.randint(0, 20) or None, "review_id_native": None,
            "reviewer_location": "India", "country_code": "IN", "variant": "Colour: Black", "images": [],
            "videos": [], "asin": asin, "marketplace": "amazon.in",
        })
    return {"product_id": asin, "url": f"https://www.amazon.in/dp/{asin}", "product_name": f"Bench earbuds {i}",
            "marketplace": "amazon.in", "scraped_at": "2026-11-24T00:00:00+00:00",
            "price": {"currency": "INR", "selling_price": 999.0}, "product_details": {"product_id": asin},
            "review_summary": {"reviews_available": n}, "reviews": reviews}


async def _stall_probe(stop: asyncio.Event, out: list) -> None:
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        t = loop.time()
        await asyncio.sleep(0.01)
        out.append(loop.time() - t - 0.01)


async def run(variant: str, products: int, per: int) -> dict:
    from app.orchestrator.engine import Orchestrator
    from app.platforms.amazon.adapter import AmazonAdapter
    from app.platforms.base import ScrapeResult
    from app.processing import language, normalizer
    from app.schemas.scraping import ScrapeJobRequest
    from app.storage.database import get_db
    from app.storage.incremental import IncrementalStore

    language._CACHE.clear()
    req = ScrapeJobRequest(platform="amazon", search={"product": "bench", "marketplace": "amazon.in"},
                           scraping_options={"date_range": {"type": "custom", "start_date": "2000-01-01",
                                                            "end_date": "2026-12-31"}})
    orch, adapter, db = Orchestrator(), AmazonAdapter(), get_db()
    await db.client.drop_database(settings.mongo_db)
    models = [model(i, per) for i in range(products)]
    stop, stalls = asyncio.Event(), []
    probe = asyncio.create_task(_stall_probe(stop, stalls))
    await asyncio.sleep(0.05)  # let the probe tick before the measured work starts
    t0 = time.perf_counter()
    if variant == "before":
        settings.language_workers = 0  # previous behaviour: serial, in-process
        records = adapter.to_records(ScrapeResult(raw_products=models), req, "bench_before")  # on the loop
        await orch._assign_identity(records, req)
        normalizer.finalize(records)
        await orch.records.upsert_many(records)
        await orch.collections.save_scraped_data(records)
    else:
        settings.language_workers = None
        store = IncrementalStore(adapter, req, "bench_after", db=db, records_repo=orch.records,
                                 collections_repo=orch.collections, assign_identity=orch._assign_identity,
                                 batch_size=settings.storage_batch_size)
        await store.start()
        for m in models:  # products arrive one by one as scraping finishes them
            await store.product_scraped("amazon", m)
            await asyncio.sleep(0)
        await store.close()
    seconds = time.perf_counter() - t0
    stop.set()
    await probe
    unique = await db["reviews"].count_documents({"platform": "amazon"})
    await db.client.drop_database(settings.mongo_db)
    return {"variant": variant, "products": products, "reviews": products * per,
            "seconds": round(seconds, 2), "reviews_per_minute": round(products * per / (seconds / 60)),
            "max_event_loop_stall_seconds": round(max(stalls or [0]), 2),
            "unique_review_docs_in_mongodb": unique}


def main() -> None:
    import logging
    logging.disable(logging.CRITICAL)
    args = sys.argv[1:]
    settings.mongo_db = args[args.index("--db") + 1] if "--db" in args else "web_data_extractor_bench"
    assert settings.mongo_db != "web_data_extractor", "never benchmark against the real database"
    scenarios = [a for a in args if a in SCENARIOS] or ["small", "medium", "large"]
    variants = [a for a in args if a in ("before", "after")] or ["before", "after"]
    if "--one" in args:  # one measurement per process: clean peak memory / CPU
        sc, v = scenarios[0], variants[0]
        cpu = procstats.cpu_seconds()
        res = asyncio.run(run(v, *SCENARIOS[sc]))
        from app.processing import language
        if language._POOL is not None:
            language._POOL.shutdown()  # so pool workers' CPU is counted
        res.update(scenario=sc, cpu_seconds_incl_workers=round(procstats.cpu_seconds() - cpu, 1),
                   peak_memory_mb_main_process=procstats.peak_memory_mb())
        print(json.dumps(res), flush=True)
        return
    for sc in scenarios:
        for v in variants:
            subprocess.run([sys.executable, "-m", "benchmarks.storage_bench", "--one", sc, v,
                            "--db", settings.mongo_db], check=True)


if __name__ == "__main__":
    main()
