# web-data-extractor

A platform-pluggable web data extraction service for scraping product data,
offers, and reviews across e-commerce platforms (Amazon, Alibaba, Best Buy,
Walmart), with normalization, history tracking, and analytics.

## Project structure

```
app/
  main.py                  # FastAPI entry point
  api/v1/                  # HTTP API (scraping, jobs, products, history, analytics)
  core/                    # config, logging, exceptions
  platforms/               # per-platform scraping adapters + registry
  orchestrator/            # job orchestration, scheduling of scrape runs
  schemas/                 # Pydantic models (product, offer, review, history)
  processing/              # normalize, validate, deduplicate, quality-check
  history/                 # snapshots + change detection
  storage/                 # database, ORM models, repositories, local storage
  workers/                 # background workers (optional)
  scheduler/               # recurring job scheduling (optional)
  analytics/               # analytics computations
local_data/                # raw dumps, exports, logs, debug, backups
migrations/                # database migrations
tests/                     # unit, adapters, integration, history
```

## Getting started

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start infrastructure (Redis + MongoDB)

A dedicated Redis for this project runs via Docker on host port **6380** (so it
does not clash with any other Redis you may have on 6379):

```bash
docker compose up -d
```

MongoDB is expected at `mongodb://localhost:27017` (override with `mongo_uri`).

Connection settings live in `.env` (already created for local dev):

```
celery_broker_url=redis://localhost:6380/0
celery_result_backend=redis://localhost:6380/1
job_executor=celery
```

### 3. Run the API

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs to try `POST /api/v1/scraping/jobs`.

### 4. Run a worker (for `job_executor=celery`)

Scrape jobs are dispatched to a Celery worker, so start one in a second terminal:

```bash
# Windows: use the solo pool (the default prefork pool is unsupported on Windows)
celery -A app.workers.celery_app worker --loglevel=info --pool=solo

# Linux/macOS
celery -A app.workers.celery_app worker --loglevel=info
```

Without a running worker, jobs are accepted and stay `queued` but never run.

### Alternative: no worker / no broker

For quick local testing you can run jobs in-process instead of via Celery — set
`job_executor=inline` in `.env`. The API then executes each job in the background
with asyncio; no Redis or worker is required.

## Platforms

Registered platforms are listed at `GET /api/v1/platforms`; each platform's
`platform_options` are described at `GET /api/v1/platforms/{platform}/config-schema`.

### Amazon

- `search.marketplace` must be a supported English storefront: `amazon.in`,
  `amazon.com`, `amazon.co.uk`, `amazon.ca`, `amazon.com.au`. If
  `filters.currency` is given it must match the marketplace currency (prices are
  never converted).
- `platform_options.amazon`: `include_sponsored`, `fulfilled_by_amazon_only`,
  `review_sort` (`recent` | `helpful`), `verified_reviews_only`.
- The engine uses its own Chrome profile (`amazon_profile_dir`, default
  `local_data/chrome_amazon_profile`). Amazon only serves full review pages
  (`/product-reviews/`) to signed-in sessions. To collect more than the reviews
  shown on the product page, sign in to Amazon manually once in that profile;
  the scraper never signs in itself. Without sign-in, jobs still complete and
  `execution_meta.actual.review_pages_login_required` reports it.
- A CAPTCHA pauses the run for manual solving in the visible browser
  (`captcha_wait_seconds`).
- Throughput settings: `amazon_detail_concurrency` (default 1),
  `amazon_max_retries`, `amazon_min_delay_seconds` / `amazon_max_delay_seconds`.

## Request shape & generated fields

The request body is intentionally small (`platform`, `search`, `filters`,
`scraping_options`, `platform_options`). Several output fields are **generated in
code** by the identifier layer, not supplied by the caller:

- `scraping_options.date_range` — filters reviews by date. Types: `monthly`,
  `quarterly`, `last_1_year`, `last_2_years`, `custom` (custom needs
  `start_date` + `end_date`). Reviews without a parseable date are kept.
- Identifiers use **UUIDv7** with platform-coded prefixes (codes centralized in
  `app/core/identifiers.py`: `ali`, `amz`, `bb`, `wmt`, `eby`, `flp`):
  - `_id` = `<code>_mnt_<uuid7>` (e.g. `ali_mnt_019a…`) — never contains a
    product id. Re-scraping the same review reuses its id (dedup by
    `platform` + `source.source_item_id`), so runs update instead of duplicating.
  - execution id / `provenance.collection_run_id` = `<code>_job_<uuid7>`.
  - `tenant.tenant_id` = `ten_<uuid7>`, `tenant.project_id` = `prj_<uuid7>` —
    platform-independent, provisioned **once** (stored in the `provisioning`
    collection) and reused across all runs. If unresolvable the job errors
    rather than using a placeholder.
- `source.source_item_id` — the platform's original review id when available,
  otherwise a clearly-marked `gen:<hash>` fallback (never the product id).
- `tenant.report_scope_ids` — calendar-quarter scopes covering the date range,
  e.g. `["TND_Q2_FY27"]` (Q1=Jan–Mar … Q4=Oct–Dec); prefix via
  `report_scope_prefix` in config.
- `record_type` — `social_mention` for reviews/feedback (the kind is carried by
  `source_type` / `channel` / `content_type`: customer `review` vs
  `supplier_feedback`), `product_listing` for the listing fallback.
- Individual reviews carry only their own `metrics.rating`; product-level
  aggregate counts are not copied onto a single review.
