# Running the service & API input reference

This guide covers how to run the web-data-extractor locally and exactly how the
`POST /api/v1/scraping/jobs` input parameters work.

## 1. Prerequisites

- Python 3.14
- Docker (for the project Redis container)
- MongoDB reachable at `mongodb://localhost:27017`
- Google Chrome installed (the scraping engines use the `chrome` channel)

## 2. Install

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (PowerShell/CMD)
# source .venv/bin/activate       # Linux/macOS
pip install -r requirements.txt
playwright install chromium
```

> If you run the app with a different Python than `.venv` (e.g. a global
> interpreter), install the requirements into that interpreter too — all the
> scrapers need `playwright`, `langdetect`, and `beautifulsoup4`.

## 3. Start infrastructure

A dedicated Redis for this project runs on host port **6380** (so it never
clashes with other Redis instances):

```bash
docker compose up -d
```

MongoDB is expected at `mongodb://localhost:27017`. Connection settings live in
`.env`:

```
celery_broker_url=redis://localhost:6380/0
celery_result_backend=redis://localhost:6380/1
job_executor=celery
```

## 4. Run the API

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive Swagger UI.

## 5. Run a worker (when `job_executor=celery`)

Jobs are dispatched to a Celery worker. In a second terminal:

```bash
# Windows (solo pool required):
.venv\Scripts\python.exe -m celery -A app.workers.celery_app worker --loglevel=info --pool=solo

# Linux/macOS:
celery -A app.workers.celery_app worker --loglevel=info
```

Without a running worker, jobs are accepted and stay `queued` but never run.
Celery does **not** hot-reload — restart the worker after changing code.

**Alternative (no worker/Redis):** set `job_executor=inline` in `.env` and jobs
run in-process via asyncio under `uvicorn`. Handy for quick local testing.

The scrapers open a real browser. To watch it, set `browser_headless=false` in
`.env` (default). Set `true` for headless runs.

## 6. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/scraping/jobs` | Submit a scrape job (returns `202` + `job_id`) |
| `GET`  | `/api/v1/jobs/{job_id}` | Poll job status, counts, export paths |
| `GET`  | `/api/v1/platforms` | List supported platforms |
| `GET`  | `/api/v1/platforms/{platform}/config-schema` | Platform-specific `platform_options` schema |
| `GET`  | `/health` | Health check |

## 7. Typical flow

```bash
# 1. submit
curl -X POST http://127.0.0.1:8000/api/v1/scraping/jobs \
  -H "Content-Type: application/json" -d @request.json
# -> {"job_id":"ali_job_<uuid7>","status":"queued",...}

# 2. poll
curl http://127.0.0.1:8000/api/v1/jobs/ali_job_<uuid7>
# -> status running -> completed; includes products_scraped, records_written,
#    exports {json, excel}, execution_meta

# 3. results
#    MongoDB: web_data_extractor.records
#    Files:   local_data/exports/<platform>/<platform>_<job_id>_<timestamp>.json / .xlsx
```

---

# API input parameters

The request body is intentionally small and identical across platforms. Only
platform-specific tuning differs, via `platform_options`.

```json
{
  "platform": "alibaba",
  "search": {
    "product": "stainless steel water bottle",
    "category": "Drinkware",
    "brand": "",
    "keywords": ["insulated", "750ml"],
    "marketplace": "alibaba.com",
    "language": "en"
  },
  "filters": {
    "min_price": 1,
    "max_price": 20,
    "currency": "USD",
    "exclude_keywords": ["sample", "used"]
  },
  "scraping_options": {
    "max_products": 20,
    "max_pages": 1,
    "sort_by": "relevance",
    "review_pages_per_product": 1,
    "date_range": { "type": "last_1_year", "start_date": null, "end_date": null }
  },
  "platform_options": {
    "alibaba": {
      "verified_supplier_only": true,
      "supplier_type": "manufacturer",
      "supplier_country": ["CN"]
    }
  }
}
```

### `platform` (required)
One of: `alibaba`, `amazon`, `flipkart`, `samsung`, `bestbuy`, `walmart`.
The request is routed to that platform's adapter.

- **Implemented:** `alibaba`, `flipkart`, `samsung`.
- **Placeholders:** `amazon`, `bestbuy`, `walmart` — accepted, but the job fails
  with `501 platform_not_implemented` (no fabricated data).

### `search` (required)
| Field | Type | Required | Meaning |
|---|---|---|---|
| `product` | string | **yes** | Main search term |
| `category` | string | no | Used to derive `taxonomy.category` on records |
| `brand` | string | no | Appended to the search query |
| `keywords` | string[] | no | Appended to the search query |
| `marketplace` | string | **yes** | Target marketplace (e.g. `alibaba.com`, `flipkart.com`) |
| `language` | string | no | Declared language; **not** forced onto scraped content (actual review language is auto-detected) |

The engines build the search query as `product + brand + keywords`.

### `filters`
| Field | Type | Meaning |
|---|---|---|
| `min_price` / `max_price` | number ≥ 0 | Price bounds; `min_price` must be ≤ `max_price` |
| `currency` | string | Recorded for reference only — **prices are preserved in the marketplace's own display currency; no conversion is performed** |
| `exclude_keywords` | string[] | Products whose title contains any of these are dropped |

### `scraping_options`
| Field | Type | Default | Range | Meaning |
|---|---|---|---|---|
| `max_products` | int | 20 | 1–500 | Upper limit on products scraped |
| `max_pages` | int | 1 | 0–50 | Max search-result pages to traverse |
| `sort_by` | enum | `relevance` | `relevance`,`price_asc`,`price_desc`,`rating`,`newest` | Search ordering |
| `review_pages_per_product` | int | 1 | 0–50 | Review pages fetched per product |
| `date_range` | object | `last_1_year` | see below | Filters reviews by publish date |

**`date_range.type`:** `monthly`, `quarterly`, `last_1_year`, `last_2_years`, `custom`.
For `custom`, both `start_date` and `end_date` (`YYYY-MM-DD`) are required and
`start_date` ≤ `end_date`. Reviews without a parseable date are kept.

### `platform_options`
A map keyed by platform name; **only the entry matching `platform` is read**, and
unknown platform keys are rejected. Fetch the live schema per platform from
`GET /api/v1/platforms/{platform}/config-schema`.

**alibaba**
| Option | Type | Default |
|---|---|---|
| `verified_supplier_only` | bool | false |
| `trade_assurance_only` | bool | false |
| `min_order_quantity` | int ≥1 | null |
| `max_order_quantity` | int ≥1 | null |
| `supplier_country` | string[] | [] (ISO codes, e.g. `["CN"]`) |
| `supplier_type` | enum | `all` (`manufacturer`,`trading_company`,`all`) |
| `include_sponsored` | bool | false |

**flipkart**
| Option | Type | Default |
|---|---|---|
| `review_sort` | enum | `recent` (`helpful`,`recent`,`positive`,`negative`) |
| `verified_purchase_only` | bool | false |
| `include_sponsored` | bool | false |

**samsung**
| Option | Type | Default |
|---|---|---|
| `category` | string | null (auto-detected from the search term; Samsung only sells Samsung — non-Samsung brand queries return an empty result) |

`amazon`, `bestbuy`, `walmart` accept no options yet.

### Fields generated in code (not request input)
These appear in the output records but are **not** accepted from the caller:

- `tenant.tenant_id` / `tenant.project_id` — UUIDv7 (`ten_…` / `prj_…`),
  provisioned once and reused.
- `tenant.report_scope_ids` — calendar-quarter scopes derived from `date_range`
  (e.g. `TND_Q2_FY27`).
- `_id` — `<platform_code>_mnt_<uuid7>` (e.g. `ali_mnt_…`); re-runs reuse the id
  for the same source item (dedup).
- `provenance.collection_run_id` — execution id `<platform_code>_job_<uuid7>`.
- `record_type` — `social_mention` for reviews/feedback, `product_listing` for
  the listing fallback.

## Validation errors
| Condition | Response |
|---|---|
| Unknown platform | `404 platform_not_supported` |
| Missing `product` / `marketplace`, bad types, `min_price > max_price`, invalid `custom` dates, unknown `platform_options` key | `422 invalid_request` |
| Unimplemented platform at run time | job `failed`, `501 platform_not_implemented` |
| Scraper failure | job `failed` with the error on `GET /jobs/{job_id}` |

## Output
Each run writes to:
- **MongoDB** `web_data_extractor.records` (canonical `SocialMentionRecord`), and
- **Per-platform files** `local_data/exports/<platform>/<platform>_<job_id>_<timestamp>.json` and `.xlsx`.

`GET /api/v1/jobs/{job_id}` returns `exports` (the file paths) and
`execution_meta` (requested-vs-actual counts and which filters were applied).
