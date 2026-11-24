# web-data-extractor

A platform-pluggable web data extraction service for scraping product data,
offers, and reviews across e-commerce platforms (Amazon, Alibaba, Best Buy,
Walmart), with normalization, history tracking, and analytics.

## Project structure

```
app/
  main.py                  # FastAPI entry point
  api/v1/                  # HTTP API (scraping, jobs, platforms)
  core/                    # config, logging, exceptions, identifiers, periods
  schemas/                 # Pydantic models (record + offer/product/review/history blocks, scraping)
  orchestrator/            # job manager, run engine, identity assignment, result aggregation
  platforms/               # per-platform scrapers + base/registry/categories/common/browser
    alibaba/ amazon/ flipkart/ samsung/ walmart/ bestbuy/
  processing/              # normalize, validate, deduplicate, quality-check, language detect
  storage/                 # database, repositories (jobs/records/provisioning), local export
  workers/                 # Celery app + background task
local_data/                # exports, debug dumps, chrome profiles
docs/                      # USAGE, TECH_STACK, ARCHITECTURE, REQUEST_EXAMPLES
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
  `review_sort` (`recent` | `helpful`), `verified_reviews_only`,
  `collect_all_reviews` (default `true`).
- Reviews: Amazon's review listing ignores URL page numbers (more reviews load
  with "Show 10 more reviews") and shows at most 100 reviews per listing. With
  `collect_all_reviews` the scraper loads every listing to its end and
  partitions the reviews (star rating, then sort order, media-only, current
  format, verified-only and keywords mined from the reviews themselves) until
  each star group's reported count is reached or keyword listings stop adding
  reviews (`amazon_review_keyword_stall_limit`). There is no review-count
  limit. Each product's `review_summary` reports `reviews_available`,
  `reviews_extracted`, `coverage_percent`, `complete`, `incomplete_reason`,
  `star_coverage` and `failed_listings`; nothing is invented when Amazon does
  not expose every review. `review_pages_per_product: 0` disables review pages;
  with `collect_all_reviews: false` it is the number of 10-review pages loaded.
- `scraping_options.date_range` still filters the stored reviews (default: last
  year); use a `custom` range to keep older ones. `review_summary.reviews_outside_date_range`
  counts what it excluded.
- The engine uses its own Chrome profile (`amazon_profile_dir`, default
  `local_data/chrome_amazon_profile`). Amazon only serves full review pages
  (`/product-reviews/`) to signed-in sessions. To collect more than the reviews
  shown on the product page, sign in to Amazon manually once in that profile;
  the scraper never signs in itself. Without sign-in, jobs still complete and
  `execution_meta.actual.review_pages_login_required` reports it.
- A CAPTCHA pauses the run for manual solving in the visible browser
  (`captcha_wait_seconds`).
- Throughput settings: `amazon_detail_concurrency` (products in parallel,
  default 2), `amazon_max_retries`, `amazon_min_delay_seconds` /
  `amazon_max_delay_seconds` (gap between page loads) and
  `amazon_review_click_min_delay` / `amazon_review_click_max_delay` (gap
  between "show more" requests). Both gaps are one budget shared by all
  concurrent products, so raising concurrency overlaps waiting but never
  raises the request rate Amazon sees (sustained traffic triggers a CAPTCHA
  wall on the session). Keyword phase: `amazon_review_keyword_stall_limit`
  (15), `amazon_review_keyword_yield_window` / `amazon_review_keyword_min_yield`
  (8 / 3: stop when the last 8 keyword listings found fewer than 3 new reviews).
- Rate limits: `amazon_max_requests_per_minute` (20) caps all Amazon requests
  (page loads + "show more") per minute across concurrent products; sustained
  ~40/min led to Amazon's 503 "rush hour" page and "Page Not Found" on review
  pages. Throttling is backed off (`amazon_throttle_backoff_seconds` 30,
  `amazon_throttle_retries` 3); once Amazon rate-limits a run, later jobs
  within `amazon_cooldown_minutes` (60, 0 = off) stop immediately with the
  time the next run is allowed (state in the Amazon profile folder,
  `.amazon_cooldown.json`).
- Statistics: `execution_meta.performance` reports per product (reviews
  available / extracted / stored, pages, listings, failed pages, requests,
  seconds) and per run (totals, failures, execution time, average per product,
  reviews per minute, requests sent).

### Incremental storage and resume

Platforms whose adapter sets `supports_incremental` (Amazon) store each product
as soon as it finishes: records and the products / retailers / reviews
collections are bulk-upserted in background batches of `storage_batch_size`
reviews (default 500), and a `.partial.jsonl` is appended next to the exports
until the final JSON is written. Only the existing `records`, `products`,
`retailers` and `reviews` collections are used: a re-run skips products already
in `products` and stored within `resume_completed_within_hours` (24); an
interrupted product is scraped again and its reviews are upserted on the unique
review key, so retries and re-runs never duplicate reviews.

Each job's JSON export (every platform) is one folder,
`<platform>_<time>_<product>_<job id>/`, holding `products.json`,
`retailers.json` and `reviews.json` - the three collections exactly as stored
in MongoDB (the full records stay in MongoDB's `records` collection; no
combined records JSON is written). Review language detection is
cached and runs in a small process pool for large batches (`language_workers`,
default CPUs-1, max 4).

Benchmarks: `python -m benchmarks.amazon_reviews_sim` (simulated Amazon, real
collection code, virtual clock) and `python -m benchmarks.storage_bench`
(real MongoDB, separate `web_data_extractor_bench` database, dropped after).

### Alibaba

- `search.marketplace` must be `alibaba.com`. `filters.currency`, if given, must
  be an ISO-4217 code; prices are recorded in the currency Alibaba displays and
  are never converted (Alibaba's own USD figures are kept as `usd_price_min/max`).
- `platform_options.alibaba`: `verified_supplier_only`, `trade_assurance_only`,
  `min_order_quantity` / `max_order_quantity`, `supplier_country`,
  `supplier_type` (`manufacturer` | `trading_company` | `all`),
  `include_sponsored`, `verified_reviews_only`.
- Output follows the Amazon contract: one `product_listing` record per product
  with the supplier's description / "Highlights at a glance" (`content.text`,
  `product_attributes.highlights`), detail-page pricing (quantity tiers, or the
  price range with its promotional range and derived discount), stock status,
  wholesale terms (`commercial.offers_detail`), and both product reviews and
  supplier (store) feedback embedded in `reviews` (told apart by `review_type`).
- `collect_all_reviews` (default `true`): product reviews and supplier
  feedback are loaded page by page until each source's last page (no page
  cap; `review_pages_per_product: 0` still disables review pages, and with
  the option off only `review_pages_per_product` pages are loaded).
  `review_summary` reports `reviews_available` / `reviews_extracted` /
  `coverage_percent` / `complete` / `incomplete_reason` for product reviews,
  and the same for supplier feedback under `store_reviews`
  (`coverage_complete`). Alibaba's displayed counts can exceed what its pages
  list; the reason then says so.
- Products whose detail page could not be loaded are kept with search-card data;
  their `review_summary.stop_reason` says why reviews are missing.
- A CAPTCHA (slider) pauses the run for manual solving in the visible browser
  (`captcha_wait_seconds`).
- Throughput settings: `review_concurrency`, `feedback_concurrency`,
  `alibaba_max_retries`, `alibaba_min_delay_seconds` / `alibaba_max_delay_seconds`.

### Flipkart

- `platform_options.flipkart`: `review_sort` (`helpful` | `recent` | `positive`
  | `negative`), `verified_purchase_only`, `include_sponsored`,
  `collect_all_reviews` (default `true`).
- Reviews: each Flipkart review listing (one sort order) stops after 10 pages /
  100 reviews, whatever the product's review count (star, aspect and
  certified-buyer URL filters do not split the reviews further). With
  `collect_all_reviews` the scraper loads all four sort orders (the requested
  `review_sort` first) to their last page, de-duplicates across them and stops
  early once the reported review count is reached. `review_summary` reports
  `reviews_available` / `reviews_extracted` / `coverage_percent` / `complete` /
  `incomplete_reason`, plus `listings` (pages, found, new per sort order) and
  `failed_listings`. Flipkart intermittently redirects review pages to its
  login page; the page is retried with back-off (4 s, 8 s, 16 s). With the
  option off (or `review_pages_per_product: 0`) only `review_pages_per_product`
  pages of `review_sort` are loaded, as before.

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
