# Multi-Retail & Samsung E-Commerce Scraper, Search & Filter Pipeline

A modular Python e-commerce web scraping, search, filtering, and persistence pipeline.
Supports **Samsung India (`samsung.com/in`)** as well as third-party retail platforms (**Croma, Reliance Digital, Vijay Sales, Flipkart, Amazon India**, or any **Schema.org / OpenGraph / JSON-LD** product URL) into **MongoDB**, **JSON**, and **CSV**.

---

## 📊 Standardized Schema v2.0.0 Output (Product Details + Review Details)

Scraped output is separated cleanly into two Schema v2.0.0 JSON files and two MongoDB collections (`products` & `reviews`):
- **Products JSON**: `output/samsung_products.json` (`products` collection)
- **Reviews JSON**: `output/samsung_reviews.json` (`reviews` collection)

### 1. Product Details Schema (`output/samsung_products.json`)

```json
[
  {
    "metadata": {
      "platform": "amazon | flipkart | bestbuy | walmart | samsung | jiomart | alibaba | ebay | target | indiamart",
      "marketplace_type": "B2C | B2B | D2C",
      "region": "US | IN | GLOBAL",
      "scraped_at": "2026-11-11T15:29:00Z",
      "schema_version": "2.0.0"
    },
    "product": {
      "identifiers": {
        "platform_product_id": "SM-S928BZKEINS",
        "parent_product_id": null,
        "sku": "SM-S928BZKEINS",
        "upc_ean": null,
        "model_number": "SM-S928BZKEINS",
        "product_url": "https://www.samsung.com/in/smartphones/galaxy-s24-ultra/buy/"
      },
      "core": {
        "title": "Galaxy S24 Ultra",
        "brand": "Samsung",
        "manufacturer": "Samsung Electronics",
        "category_path": ["Electronics", "Smartphones"],
        "condition": "new",
        "introduced_date": "January 17, 2024"
      },
      "pricing_and_availability": {
        "currency": "INR",
        "list_price": 134999.0,
        "current_price": 129999.0,
        "discount_percentage": 3.7,
        "is_on_sale": true,
        "b2b_price_tiers": [],
        "moq": 1,
        "in_stock": true,
        "stock_status_text": "In Stock",
        "fulfillment": {
          "is_platform_fulfilled": true,
          "free_shipping": true,
          "estimated_delivery_days": null
        }
      },
      "content_and_specs": {
        "short_description": "...",
        "bullet_features": [],
        "specifications": {},
        "included_items": [],
        "variants": [],
        "b2b_customization": {
          "oem_odm_available": false,
          "custom_logo": false,
          "custom_packaging": false
        }
      },
      "media": {
        "images": [{ "url": "https://...", "type": "main", "position": 1 }],
        "videos": [{ "video_id": "vid_001", "url": "https://...", "thumbnail_url": null, "duration_seconds": null }]
      },
      "seller": {
        "seller_id": null,
        "seller_name": "Samsung Official Store",
        "store_url": "https://www.samsung.com/in/",
        "is_official_brand_store": true,
        "is_verified_seller": true,
        "years_active": null,
        "seller_rating": null,
        "on_time_delivery_rate": null,
        "response_rate": null
      }
    }
  }
]
```

### 2. Review Details Schema (`output/samsung_reviews.json`)

```json
[
  {
    "metadata": {
      "platform": "samsung",
      "platform_product_id": "SM-S928BZKEINS",
      "parent_product_id": null,
      "seller_id": null,
      "scraped_at": "2026-11-11T15:32:00Z",
      "schema_version": "2.0.0"
    },
    "reviews": {
      "product_reviews": {
        "summary": {
          "platform_product_id": "SM-S928BZKEINS",
          "average_rating": 4.75,
          "total_ratings": 124,
          "total_written_reviews": 98,
          "recommended_percentage": 94.0,
          "star_distribution": { "5": 105, "4": 12, "3": 4, "2": 1, "1": 2 },
          "aspect_ratings": { "Battery Life": 4.8, "Camera": 4.9 }
        },
        "items": [
          {
            "review_id": "R3892716450",
            "platform_product_id": "SM-S928BZKEINS",
            "rating": 5,
            "title": "Unbelievable Camera and Display",
            "body": "The zoom quality and flat display are exceptional.",
            "published_at": "2026-10-24T18:42:11Z",
            "language": "en",
            "is_verified_purchase": true,
            "is_incentivized_or_default": false,
            "is_recommended": true,
            "helpful_votes": 12,
            "reviewer": {
              "user_id": "karthikr92",
              "name": "Karthik R.",
              "country_code": "IN",
              "badges": ["Verified Purchase"]
            },
            "purchased_variant": { "variant_id": null, "attributes_text": null },
            "tags": { "pros": [], "cons": [] },
            "media": { "images": [], "videos": [] },
            "comments": []
          }
        ]
      },
      "store_reviews": {
        "summary": {
          "seller_id": null,
          "average_store_rating": null,
          "total_store_reviews": 0,
          "sub_scores": { "seller_service": null, "shipping_speed": null, "item_accuracy": null }
        },
        "items": []
      }
    }
  }
]
```

### 🌟 Canonical Schema Rules
1. **`metadata`**: Every Product Details & Review Details record includes `platform`, `marketplace_type` (`B2C | B2B | D2C`), `region` (`US | IN | GLOBAL`), ISO UTC `scraped_at`, and `schema_version` (`"2.0.0"`).
2. **`product.pricing_and_availability`**: Authentic extracted pricing only (`list_price`, `current_price`, `discount_percentage`, `is_on_sale`, `b2b_price_tiers`, `moq`, `in_stock`, `fulfillment`). Absent metrics remain `null` / `[]`.
3. **`reviews`**: Consolidated `product_reviews` (`summary` with `star_distribution` `"5".."1"` + `aspect_ratings`, `items` with verified/recommended/media/comments) alongside `store_reviews`.
7. **Backward Compatibility**: Products with `category` in `["laptops", "computers"]` also include `"laptop_name"`.

---

## 🌐 Multi-Retail Platforms & Honest Limitations

Pass any supported platform via `--platform` (supports aliases like `"wall mart"`, `"jio"`, `"best buy"`):
- `samsung` (Samsung India — full XML sitemap + Bazaarvoice reviews)
- `croma` (`croma.com`)
- `reliancedigital` (`reliancedigital.in`)
- `vijaysales` (`vijaysales.com`)
- `flipkart` (`flipkart.com`)
- `amazon` (`amazon.in`)
- `alibaba` (`alibaba.com`, USD)
- `walmart` (`walmart.com`, USD)
- `jiomart` (`jiomart.com`, INR — aliases: `jio`, `jiomart`)
- `bestbuy` (`bestbuy.com`, USD — aliases: `bestbuy`, `best buy`)
- `generic` / `auto` (works on **any** retail URL exposing Schema.org `Product` JSON-LD, OpenGraph meta tags, or standard retail price DOM markup)

You can also search or list platforms interactively or via CLI:
```powershell
python main.py --interactive              # Two-step wizard: (1) search platform -> (2) search & filter products
python main.py --list-platforms           # List all supported retail platforms
python main.py --find-platform "best buy" # Search configured platforms by keyword/alias
```

### Important Practical Notes
- **JavaScript-Rendered Search/SPAs**: Stores that render search cards purely client-side may return 0 static HTML links in fast HTTP mode; pass `--playwright` (headless Firefox) or `--visual`, or pass direct product URLs via `--url` / `--urls`.
- **CAPTCHA & Anti-Bot Gates**: Large marketplaces (e.g., Amazon, Flipkart, Cloudflare-protected storefronts) frequently gate automated requests with HTTP 403/429/503 or CAPTCHA challenges. The scraper detects anti-bot gates cleanly and stops/reports the block rather than attempting to bypass them.
- **Undated Product Pages**: Third-party stores often do not publish launch dates (`releaseDate` / `datePublished`) in page HTML. Because `--days 365` (last 1 year) is enabled by default, pass `--all-time` or `--include-undated` when scraping generic retail pages without dates.

---

## 📁 Output Storage Files

- **JSON**: `output/samsung_products.json` (and `output/samsung_laptops.json`)
- **CSV**: `output/samsung_products.csv` (and `output/samsung_laptops.csv`)
- **Reviews CSV**: `output/samsung_product_reviews.csv`

---

## 🚀 Usage Examples (`main.py`)

### 1. Scrape & Filter Samsung Products (Default: Last 1 Year `--days 365`)
```powershell
# Smartphones from the last 1 year priced at least ₹20,000:
python main.py --batch --platform samsung --category smartphones --days 365 --min-price 20000 --limit 5

# Search a specific product family with price & rating filters:
python main.py --batch --platform samsung --search "galaxy book" --min-price 50000 --max-price 150000 --min-rating 4.0 --sort-by rating_desc

# Include products from all time (disable 1-year date cutoff):
python main.py --batch --platform samsung --category laptops --all-time --limit 10
```

### 2. Scrape Different Retail Platforms or Any Direct Product URL
```powershell
# Search on another retail platform (with Playwright JS rendering):
python main.py --batch --platform croma --search "galaxy" --playwright --all-time --limit 3

# Scrape specific URL(s) from any Schema.org/OpenGraph website (auto-detects platform):
python main.py --url "https://www.samsung.com/in/audio-devices/music-frame/ls60d-black-hw-ls60d-xl/" --all-time
python main.py --urls "https://store-a.com/p/1" "https://store-b.com/p/2" --all-time --include-undated
```

### 3. Visual Interactive Browser Navigation
```powershell
python main.py
python main.py --visual --platform samsung --search "smartphones" --limit 3
# Non-Samsung brand search on Samsung.com cleanly returns [] (Empty List):
python main.py --headless --search "hp laptops"
```

---

## 🔍 Search & Filter Stored Data (`query_db.py`)

Query either **MongoDB** (`--source mongo`, default) or local **JSON** (`--source json --file ...`) using the exact same filter flags:

```powershell
# Filter last 1-year laptops with rating >= 4.0 sorted highest-rated first:
python query_db.py --source json --file output/samsung_laptops.json --days 365 --min-rating 4.0 --sort-by rating_desc

# Filter MongoDB by platform, brand, price range, date window, and export results:
python query_db.py --platform samsung --category smartphones --min-price 30000 --max-price 90000 --from-date 2025-01-01 --export output/filtered_phones.json

# View full JSON for one product ID:
python query_db.py --id SM-S928BZKEINS
```

### Available Search & Filter Flags
- `--platform` (`samsung`, `croma`, `reliancedigital`, `vijaysales`, `flipkart`, `amazon`, `alibaba`, `walmart`, `jiomart`, `bestbuy`, `generic`, `auto`, `all`, plus aliases like `"wall mart"`, `"jio"`, `"best buy"`)
- `--search` (multi-word keyword search across product name, ID, brand, category, and URL)
- `--category`, `--brand`
- `--days` (default: `365` for last 1 year), `--all-time`, `--from-date YYYY-MM-DD`, `--to-date YYYY-MM-DD`, `--include-undated`
- `--min-price`, `--max-price`, `--in-stock`
- `--min-rating`, `--min-reviews`
- `--sort-by` (`price_asc`, `price_desc`, `rating_desc`, `reviews_desc`, `date_desc`, `name_asc`)
- `--limit`
