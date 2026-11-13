"""
Walmart Dynamic Product + Review Scraper
----------------------------------------
- No single hard-coded product/search URL.
- Enter search terms/categories interactively when the scraper starts.
- Uses Playwright + your installed Chrome through CDP.
- Reads Walmart's page data when available and uses visible DOM text as fallback.
- Saves product data and reviews in JSON/CSV.
- Does not bypass CAPTCHA, login walls, rate limits, or access controls.
"""

import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timezone
import urllib.request
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse, quote

from playwright.sync_api import sync_playwright


SEARCH_QUERIES = []

PRODUCTS_PER_QUERY = 40       # maximum products to collect per search phrase
MAX_SEARCH_PAGES = 2           # Walmart search results pages per phrase
MAX_REVIEW_PAGES = 5           # set None to collect all review pages Walmart serves
REVIEW_SORT = "submission-desc" # newest first

FETCH_PRODUCT_DETAILS = True
SCRAPE_REVIEWS = True

# Browser
CHROME_DEBUG_PORT = 9222
CHROME_PROFILE = os.path.abspath("walmart_chrome_profile")
PAGE_WAIT_MS = 3500
DELAY_BETWEEN_PAGES_MS = 2500
DELAY_BETWEEN_PRODUCTS_MS = 1800

# Output
OUTPUT_DIR = "walmart_output"
PRODUCT_JSON = os.path.join(OUTPUT_DIR, "walmart_standardized_social_mentions.json")
REVIEWS_JSON = os.path.join(OUTPUT_DIR, "walmart_standardized_reviews.json")
PRODUCT_CSV = os.path.join(OUTPUT_DIR, "walmart_products.csv")
REVIEWS_CSV = os.path.join(OUTPUT_DIR, "walmart_reviews.csv")
DEBUG_DIR = os.path.join(OUTPUT_DIR, "debug")


def find_chrome():
    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif platform.system() == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    raise FileNotFoundError(
        "Google Chrome was not found. Update find_chrome() with your Chrome path."
    )


def launch_chrome():
    os.makedirs(CHROME_PROFILE, exist_ok=True)

    command = [
        find_chrome(),
        f"--remote-debugging-port={CHROME_DEBUG_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.walmart.com/",
    ]
    process = subprocess.Popen(command)

    for _ in range(30):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{CHROME_DEBUG_PORT}/json/version",
                timeout=2
            )
            return process
        except Exception:
            time.sleep(1)

    process.terminate()
    raise RuntimeError("Chrome did not expose its debugging port in time.")


class BrowserSession:
    def __init__(self, playwright):
        self.playwright = playwright
        self.process = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        self.process = launch_chrome()
        self.browser = self.playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CHROME_DEBUG_PORT}"
        )
        self.context = self.browser.contexts[0]
        self.page = (
            self.context.pages[0]
            if self.context.pages
            else self.context.new_page()
        )

    def close(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

def first_value(*values):
    for value in values:
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def unique(values):
    result = []
    seen = set()
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def to_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0).replace(",", ""))
    return int(number) if number.is_integer() else number


def find_values_by_key(obj, wanted_keys):
    """Collect values for matching keys anywhere in a nested Walmart JSON object."""
    found = []
    wanted = {key.lower() for key in wanted_keys}

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in wanted and child not in (None, "", [], {}):
                    found.append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return found


def find_first_key(obj, wanted_keys):
    values = find_values_by_key(obj, wanted_keys)
    return values[0] if values else None


def collect_media_urls(obj, extensions):
    urls = []

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and value.startswith("http"):
            if re.search(extensions, value, re.IGNORECASE):
                urls.append(value)

    walk(obj)
    return unique(urls)


def save_debug(name, data):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, name)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    print("Debug data saved:", path)


def make_search_url(search_query, page_number):
    """
    Build a Walmart search URL from a search phrase.
    The URL is generated dynamically for every configured query/page.
    """
    query_params = {
        "q": search_query,
        "page": page_number,
    }
    return "https://www.walmart.com/search?" + urlencode(query_params)


def get_page_props(page, url):
    print("\nOpening:", url)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(PAGE_WAIT_MS)

    title = ""
    body = ""
    try:
        title = page.title().lower()
        body = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        pass

    challenge_phrases = [
        "robot or human",
        "verify your identity",
        "verify you are",
        "captcha",
        "unusual traffic",
        "press & hold",
    ]
    if any(text in title or text in body for text in challenge_phrases):
        print(
            "Walmart verification/access challenge detected. "
            "Complete it manually in the browser if Walmart offers that option."
        )
        while True:
            page.wait_for_timeout(2000)
            try:
                title = page.title().lower()
                body = page.locator("body").inner_text(timeout=3000).lower()
            except Exception:
                break
            if not any(x in title or x in body for x in challenge_phrases):
                break

    script_tag = page.locator("script#__NEXT_DATA__")
    if script_tag.count():
        try:
            next_data = json.loads(script_tag.first.inner_text())
            return next_data.get("props", {}).get("pageProps", {})
        except Exception as exc:
            print("Could not parse __NEXT_DATA__:", exc)

    print("Walmart page JSON was not available; DOM fallback will be used.")
    return None

def get_search_items(page_props):
    if not isinstance(page_props, dict):
        return []

    initial_data = page_props.get("initialData") or {}
    search_result = initial_data.get("searchResult") or {}
    item_stacks = search_result.get("itemStacks") or []

    products = []
    seen = set()

    for stack in item_stacks:
        if not isinstance(stack, dict):
            continue
        for item in stack.get("items") or []:
            if not isinstance(item, dict):
                continue
            product_id = first_value(item.get("usItemId"), item.get("id"))
            if not product_id:
                continue
            product_id = str(product_id)
            if product_id in seen:
                continue
            seen.add(product_id)
            products.append(item)

    return products


def get_product_id_from_url(url):
    if not url:
        return None

    parsed = urlparse(url)
    if "/sp/track" in parsed.path:
        redirect_url = parse_qs(parsed.query).get("rd", [None])[0]
        if redirect_url:
            url = redirect_url
            parsed = urlparse(url)

    match = re.search(r"/ip/(?:[^/?#]+/)?(\d+)", parsed.path)
    return match.group(1) if match else None


def get_dom_product_cards(page):
    """
    Fallback for search pages where Walmart's embedded JSON is unavailable.
    Extracts visible product links and nearby card text.
    """
    try:
        return page.evaluate(
            """() => {
                const output = [];
                const links = document.querySelectorAll("a[href*='/ip/']");
                const seen = new Set();

                for (const link of links) {
                    const href = link.href || "";
                    const match = href.match(/\\/ip\\/(?:[^/?#]+\\/)?(\\d+)/);
                    if (!match) continue;

                    const id = match[1];
                    if (seen.has(id)) continue;
                    seen.add(id);

                    let card = link;
                    for (let i = 0; i < 5 && card.parentElement; i++) {
                        card = card.parentElement;
                        const text = (card.innerText || "").trim();
                        if (text.length > 30 && text.length < 2500) break;
                    }

                    output.push({
                        id,
                        href,
                        text: (card.innerText || link.innerText || "").trim(),
                        name: (link.innerText || "").trim()
                    });
                }
                return output;
            }"""
        )
    except Exception as exc:
        print("DOM product-card extraction failed:", exc)
        return []


def get_item_images(item):
    image_values = find_values_by_key(
        item, ["thumbnailUrl", "url", "src", "imageUrl"]
    )
    images = []
    for value in image_values:
        if isinstance(value, str) and value.startswith("http"):
            if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", value, re.I):
                images.append(value)
        elif isinstance(value, dict):
            candidate = first_value(value.get("url"), value.get("src"))
            if candidate:
                images.append(candidate)
    return unique(images)


def item_product_url(item):
    canonical = first_value(item.get("canonicalUrl"), item.get("productUrl"))
    if canonical:
        return canonical if canonical.startswith("http") else (
            "https://www.walmart.com/" + canonical.lstrip("/")
        )
    product_id = first_value(item.get("usItemId"), item.get("id"))
    return f"https://www.walmart.com/ip/{product_id}" if product_id else None


def get_item_price(item):
    price_info = item.get("priceInfo") or {}
    current = price_info.get("currentPrice") or {}
    price_range = price_info.get("priceRange") or {}
    return first_value(
        current.get("priceString"),
        current.get("price"),
        price_info.get("linePrice"),
        price_range.get("priceString"),
        price_range.get("minPrice"),
        item.get("priceString"),
        item.get("price"),
    )


def product_from_search_item(item, position, query):
    rating = first_value(item.get("averageRating"), item.get("rating"))
    rating_count = first_value(
        item.get("numberOfReviews"), item.get("ratingCount")
    )

    return {
        "Search query": query,
        "Position": position,
        "Product ID": str(first_value(item.get("usItemId"), item.get("id"))),
        "Product name": first_value(item.get("name"), item.get("title")),
        "Brand": first_value(
            item.get("brand"),
            (item.get("brandName") if isinstance(item.get("brandName"), str) else None),
        ),
        "Price": get_item_price(item),
        "Product rating": rating,
        "Number of ratings": rating_count,
        "Product URL": item_product_url(item),
        "Product images": get_item_images(item),
        "Product videos": [],
        "About this item": None,
        "Specifications": {},
        "Customer reviews": [],
        "_raw_product": item,
    }


def product_from_dom_card(card, position, query):
    text = card.get("text") or ""
    name = card.get("name") or None

    price_match = re.search(r"\$\s?\d[\d,]*(?:\.\d{2})?", text)
    rating_match = re.search(r"([0-5]\.\d)\s*out of\s*5", text, re.I)
    review_match = re.search(r"([\d,]+)\s*(?:ratings?|reviews?)", text, re.I)

    product_id = card.get("id")
    return {
        "Search query": query,
        "Position": position,
        "Product ID": product_id,
        "Product name": name,
        "Brand": None,
        "Price": price_match.group(0) if price_match else None,
        "Product rating": rating_match.group(1) if rating_match else None,
        "Number of ratings": (
            review_match.group(1).replace(",", "") if review_match else None
        ),
        "Product URL": card.get("href"),
        "Product images": [],
        "Product videos": [],
        "About this item": None,
        "Specifications": {},
        "Customer reviews": [],
        "_raw_product": {},
    }


def extract_visible_label_values(page):
    """
    Capture visible label/value pairs where the page exposes them through
    common HTML structures. Walmart may change its DOM, so this is best-effort.
    """
    try:
        return page.evaluate(
            """() => {
                const result = {};
                const clean = s => (s || "").replace(/\\s+/g, " ").trim();

                document.querySelectorAll("dt").forEach(dt => {
                    const label = clean(dt.innerText);
                    const value = clean(dt.nextElementSibling?.innerText);
                    if (label && value) result[label] = value;
                });

                document.querySelectorAll("tr").forEach(row => {
                    const cells = [...row.querySelectorAll("th,td")]
                        .map(cell => clean(cell.innerText))
                        .filter(Boolean);
                    if (cells.length >= 2) result[cells[0]] = cells.slice(1).join(" | ");
                });

                document.querySelectorAll("[data-testid]").forEach(el => {
                    const label = el.getAttribute("aria-label");
                    const value = clean(el.innerText);
                    if (label && value) result[label] = value;
                });

                return result;
            }"""
        )
    except Exception:
        return {}


def extract_visible_text_sections(page):
    try:
        return page.evaluate(
            """() => {
                const selectors = [
                    '[data-testid*="product-description"]',
                    '[data-testid*="about"]',
                    '[data-testid*="specification"]',
                    '[data-testid*="feature"]'
                ];
                const values = [];
                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach(el => {
                        const text = (el.innerText || "").trim();
                        if (text) values.push(text);
                    });
                }
                return [...new Set(values)];
            }"""
        )
    except Exception:
        return []


def extract_dom_media(page):
    try:
        return page.evaluate(
            """() => {
                const images = [...document.querySelectorAll("img")]
                    .map(x => x.currentSrc || x.src)
                    .filter(x => x && x.startsWith("http"));
                const videos = [...document.querySelectorAll("video, video source")]
                    .map(x => x.currentSrc || x.src)
                    .filter(x => x && x.startsWith("http"));
                return {images: [...new Set(images)], videos: [...new Set(videos)]};
            }"""
        )
    except Exception:
        return {"images": [], "videos": []}



def extract_rating_summary(page):
    """Extract Walmart's rating summary and star histogram from rendered DOM."""
    result = {
        "Rating": None, "Number of Ratings": None, "Number of Reviews": None,
        "Rating Distribution": {}, "Rating Distribution Percentages": {}
    }
    try:
        page.evaluate("""() => {
            const nodes = [...document.querySelectorAll('h1,h2,h3,h4,div,section')]
                .filter(el => (el.innerText || '').trim().toLowerCase()
                    .includes('customer ratings & reviews'));
            nodes.sort((a,b) => (a.innerText || '').length - (b.innerText || '').length);
            if (nodes[0]) nodes[0].scrollIntoView({block:'center'});
        }""")
        page.wait_for_timeout(700)
    except Exception:
        pass

    row_texts = []
    try:
        row_texts = page.evaluate("""() => {
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
            const nodes = [...document.querySelectorAll('a,button,div,span,li,p')]
                .filter(el => {
                    const t = clean(el.innerText);
                    return t.length > 0 && t.length < 100 &&
                        /^[1-5]\\s*stars?\\b/i.test(t) &&
                        /\\d{1,3}\\s*%/.test(t) && /\\([\\d,]+\\)/.test(t);
                });
            return nodes.filter(el => !nodes.some(other => other !== el &&
                el.contains(other) && clean(other.innerText).length > 0))
                .map(el => clean(el.innerText));
        }""")
    except Exception:
        row_texts = []

    try:
        body_text = page.locator("body").inner_text(timeout=8000)
    except Exception:
        body_text = ""
    normalized = re.sub(r"\s+", " ", body_text).strip()

    avg = re.search(r"([0-5](?:\.\d+)?)\s*out\s*of\s*5", normalized, re.I)
    if avg:
        result["Rating"] = float(avg.group(1))

    counts = re.search(
        r"([\d,]+)\s+ratings?\s*[|·•/]\s*([\d,]+)\s+reviews?", normalized, re.I
    ) or re.search(r"([\d,]+)\s+ratings?\s+([\d,]+)\s+reviews?", normalized, re.I)
    if counts:
        result["Number of Ratings"] = int(counts.group(1).replace(",", ""))
        result["Number of Reviews"] = int(counts.group(2).replace(",", ""))

    distribution_text = " ".join(row_texts) if row_texts else normalized
    patterns = (
        r"([1-5])\s*stars?\s*[:\-]?\s*(\d{1,3})\s*%\s*\(\s*([\d,]+)\s*\)",
        r"([1-5])\s*stars?.{0,35}?(\d{1,3})\s*%\s*\(\s*([\d,]+)\s*\)"
    )
    matches = []
    for pattern in patterns:
        matches = list(re.finditer(pattern, distribution_text, re.I))
        if matches:
            break
    for match in matches:
        stars, percentage, count = match.groups()
        result["Rating Distribution"][f"{int(stars)} Stars"] = int(count.replace(",", ""))
        result["Rating Distribution Percentages"][f"{int(stars)} Stars"] = int(percentage)
    if result["Rating Distribution"]:
        for stars in range(1, 6):
            result["Rating Distribution"].setdefault(f"{stars} Stars", 0)
            result["Rating Distribution Percentages"].setdefault(f"{stars} Stars", 0)
    return result


def enrich_product(page, product):
    product_url = product.get("Product URL")
    if not product_url:
        return

    props = get_page_props(page, product_url)
    # Walmart's rating histogram is often rendered in the visible page DOM
    # rather than in the product object inside __NEXT_DATA__.
    product["_rating_summary"] = extract_rating_summary(page)
    media = extract_dom_media(page)
    visible_labels = extract_visible_label_values(page)
    visible_sections = extract_visible_text_sections(page)

    # Read product details from Walmart's page data if present.
    product_data = {}
    if isinstance(props, dict):
        initial = props.get("initialData") or {}
        data = initial.get("data") or {}
        product_data = data.get("product") or {}

    if product_data:
        product["_raw_detail_product"] = product_data
        product["Brand"] = first_value(
            product.get("Brand"),
            product_data.get("brand"),
            product_data.get("brandName"),
        )
        product["Product name"] = first_value(
            product.get("Product name"),
            product_data.get("name"),
            product_data.get("title"),
        )

        description = first_value(
            product_data.get("shortDescription"),
            product_data.get("description"),
            product_data.get("longDescription"),
        )
        product["About this item"] = first_value(
            description, product["About this item"]
        )

        spec_values = first_value(
            product_data.get("specifications"),
            product_data.get("specification"),
            product_data.get("productAttributes"),
        )
        if isinstance(spec_values, (dict, list)):
            product["Specifications"] = spec_values

        image_info = product_data.get("imageInfo") or {}
        detail_images = []
        for image in image_info.get("allImages") or []:
            if isinstance(image, dict):
                url = first_value(image.get("url"), image.get("src"))
                if url:
                    detail_images.append(url)
            elif isinstance(image, str):
                detail_images.append(image)
        thumb = image_info.get("thumbnailUrl")
        if thumb:
            detail_images.append(thumb)

        product["Product images"] = unique(
            detail_images + product.get("Product images", []) + media["images"]
        )
        product["Product videos"] = unique(
            collect_media_urls(product_data, r"\.(mp4|m3u8|webm)(\?|$)")
            + media["videos"]
        )

        price_info = product_data.get("priceInfo") or {}
        current = price_info.get("currentPrice") or {}
        product["Price"] = first_value(
            product.get("Price"),
            current.get("priceString"),
            current.get("price"),
        )
        product["Product rating"] = first_value(
            product.get("Product rating"),
            product_data.get("averageRating"),
        )
        product["Number of ratings"] = first_value(
            product.get("Number of ratings"),
            product_data.get("numberOfReviews"),
        )

    else:
        product["Product images"] = unique(
            product.get("Product images", []) + media["images"]
        )
        product["Product videos"] = unique(
            product.get("Product videos", []) + media["videos"]
        )

    # Keep visible website labels and their displayed values.
    product["Website displayed details"] = visible_labels

    if not product.get("About this item") and visible_sections:
        product["About this item"] = visible_sections

    # If the site exposes a brand in visible text, use it only when labelled.
    if not product.get("Brand"):
        for label, value in visible_labels.items():
            if label.strip().lower() == "brand":
                product["Brand"] = value
                break

def find_review_array(obj):
    """Find a review list in Walmart's nested page data."""
    candidates = find_values_by_key(
        obj,
        ["customerReviews", "reviews", "reviewList", "reviewItems"]
    )
    for candidate in candidates:
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
            if any(
                key in candidate[0]
                for key in ("reviewText", "reviewTitle", "reviewId", "rating")
            ):
                return candidate
    return []


def parse_review(raw):
    photos = []
    for photo in raw.get("photos") or raw.get("reviewPhotos") or []:
        if isinstance(photo, str):
            photos.append(photo)
        elif isinstance(photo, dict):
            url = first_value(
                photo.get("url"),
                ((photo.get("sizes") or {}).get("normal") or {}).get("url"),
                photo.get("src"),
            )
            if url:
                photos.append(url)

    badges = []
    for badge in raw.get("badges") or []:
        if isinstance(badge, str):
            badges.append(badge)
        elif isinstance(badge, dict):
            value = first_value(
                badge.get("text"), badge.get("badgeType"), badge.get("id")
            )
            if value:
                badges.append(value)

    return {
        "Review ID": first_value(raw.get("reviewId"), raw.get("id")),
        "Reviewer name": first_value(
            raw.get("userNickname"), raw.get("reviewerName"), raw.get("userName")
        ),
        "Rating": raw.get("rating"),
        "Review title": first_value(raw.get("reviewTitle"), raw.get("title")),
        "Review text": first_value(raw.get("reviewText"), raw.get("text")),
        "Review date": first_value(
            raw.get("reviewSubmissionTime"), raw.get("submissionTime"), raw.get("date")
        ),
        "Verified purchase": any("verified" in str(x).lower() for x in badges),
        "Review badges": badges,
        "Helpful votes": first_value(
            raw.get("positiveFeedback"), raw.get("helpfulVotes")
        ),
        "Not helpful votes": first_value(
            raw.get("negativeFeedback"), raw.get("notHelpfulVotes")
        ),
        "Review photos": unique(photos),
    }


def review_page_url(product_id, page_number):
    params = {
        "page": page_number,
        "sort": REVIEW_SORT,
    }
    return (
        f"https://www.walmart.com/reviews/product/{product_id}?"
        + urlencode(params)
    )


def extract_reviews_from_dom(page):
    """Fallback for review cards rendered in Walmart's visible DOM."""
    try:
        rows = page.evaluate("""() => {
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
            const selectors = ['[data-testid*="review"]',
                '[data-testid*="Review"]', '[itemprop="review"]', 'article'];
            const candidates = [];
            for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                    const text = clean(el.innerText);
                    if (text.length >= 20 && text.length <= 5000)
                        candidates.push(text);
                }
            }
            return [...new Set(candidates)];
        }""")
    except Exception:
        return []
    output = []
    for text in rows:
        rating = re.search(r"\b([1-5])\s*(?:out of 5|stars?)\b", text, re.I)
        date = re.search(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b",
            text, re.I
        )
        if not rating and not date:
            continue
        output.append({
            "Review ID": None, "Reviewer name": None,
            "Rating": int(rating.group(1)) if rating else None,
            "Review title": None, "Review text": text,
            "Review date": date.group(0) if date else None,
            "Verified purchase": "verified purchase" in text.lower(),
            "Review badges": [], "Helpful votes": None,
            "Not helpful votes": None, "Review photos": []
        })
    return output


def scrape_product_reviews(page, product):
    product_id = product.get("Product ID")
    if not product_id:
        return

    collected = []
    seen = set()
    page_number = 1

    while MAX_REVIEW_PAGES is None or page_number <= MAX_REVIEW_PAGES:
        url = review_page_url(product_id, page_number)
        props = get_page_props(page, url)
        raw_reviews = find_review_array(props) if props else []
        parsed_reviews = [parse_review(raw) for raw in raw_reviews]
        if not parsed_reviews:
            parsed_reviews = extract_reviews_from_dom(page)
        if not parsed_reviews:
            if page_number == 1 and props:
                save_debug(f"reviews_{product_id}.json", props)
            break

        new_count = 0
        for review in parsed_reviews:
            key = review["Review ID"] or (
                review["Reviewer name"],
                review["Review date"],
                review["Review text"],
            )
            if key in seen:
                continue
            seen.add(key)
            collected.append(review)
            new_count += 1

        print(
            f"    Review page {page_number}: "
            f"{new_count} new reviews; total {len(collected)}"
        )

        if new_count == 0:
            break

        page_number += 1
        page.wait_for_timeout(1200)

    product["Customer reviews"] = collected



REVIEW_TOPIC_KEYWORDS = {
    "Delivery experience": [
        "delivery", "delayed", "late", "arrived", "shipping", "driver",
        "delivered", "not received"
    ],
    "Returns and refunds": [
        "return", "refund", "exchange", "replacement", "pickup"
    ],
    "Product quality": [
        "defect", "broken", "faulty", "quality", "stopped working",
        "durability", "damaged", "not working"
    ],
    "Packaging": [
        "packaging", "package", "box", "damaged box", "missing item"
    ],
    "Price and offers": [
        "price", "discount", "offer", "deal", "expensive", "cheap",
        "promotion", "coupon"
    ],
    "Trust and authenticity": [
        "fake", "counterfeit", "genuine", "authentic", "duplicate",
        "not original"
    ],
    "Installation and service": [
        "installation", "technician", "service", "repair", "warranty"
    ],
}


def add_review_topic_indicators(product):
    for review in product.get("Customer reviews", []):
        text = " ".join([
            str(review.get("Review title") or ""),
            str(review.get("Review text") or ""),
        ]).lower()

        topics = []
        for topic, keywords in REVIEW_TOPIC_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                topics.append(topic)

        review["Review topics (keyword indicators)"] = topics

def parse_price_number(value):
    number = to_number(value)
    return float(number) if number is not None else None


def get_spec_value(specs, *labels):
    """Find a specification by Walmart's displayed label (case-insensitive)."""
    if not isinstance(specs, dict):
        return None
    lowered = {str(k).strip().lower(): v for k, v in specs.items()}
    for label in labels:
        value = lowered.get(label.strip().lower())
        if value not in (None, "", [], {}):
            return value
    return None


def normalize_specifications(product):
    """
    Keep Walmart's own specification labels and values.
    Visible label/value pairs are preferred; structured page specifications
    are also retained without renaming their labels.
    """
    output = []
    seen = set()

    sources = [
        product.get("Website displayed details"),
        product.get("Specifications")
    ]

    def add(label, value):
        if value in (None, "", [], {}):
            return
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        label = str(label).strip()
        value = str(value).strip()
        key = (label.lower(), value)
        if label and value and key not in seen:
            seen.add(key)
            output.append({
                "Specification Name": label,
                "Specification Value": value
            })

    for source_data in sources:
        if isinstance(source_data, dict):
            for label, value in source_data.items():
                add(label, value)
        elif isinstance(source_data, list):
            for item in source_data:
                if isinstance(item, dict):
                    label = first_value(
                        item.get("name"), item.get("label"),
                        item.get("key"), item.get("title")
                    )
                    value = first_value(
                        item.get("value"), item.get("displayValue"),
                        item.get("values"), item.get("valueName")
                    )
                    if label:
                        add(label, value)

    return output



def detect_product_category(product):
    """Identify product family from search phrase and product title."""
    text = " ".join([
        str(product.get("Search query") or ""),
        str(product.get("Product name") or ""),
        str(product.get("About this item") or ""),
    ]).lower()
    if re.search(r"\b(air conditioner|air-conditioner|mini split|window ac|portable ac|ductless ac)\b", text):
        return "air_conditioner"
    if re.search(r"\b(refrigerator|fridge|freezer)\b", text):
        return "refrigerator"
    if re.search(r"\b(laptop|notebook computer|chromebook)\b", text):
        return "laptop"
    return "general"


def _flatten_category_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_flatten_category_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_category_text(v) for v in value)
    return str(value)


def extract_category_specific_details(product):
    """
    Extract category-specific attributes from Walmart's actual label/value
    specifications, then use clearly stated product title/description details.
    Missing attributes are omitted; values are never guessed.
    """
    category = detect_product_category(product)
    sources = []
    for source in (product.get("Website displayed details"), product.get("Specifications")):
        if isinstance(source, dict):
            sources.append({str(k).strip().lower(): v for k, v in source.items()})
        elif isinstance(source, list):
            mapped = {}
            for item in source:
                if not isinstance(item, dict):
                    continue
                label = first_value(item.get("name"), item.get("label"), item.get("key"), item.get("title"))
                value = first_value(item.get("value"), item.get("displayValue"), item.get("values"), item.get("valueName"))
                if label and value not in (None, "", [], {}):
                    mapped[str(label).strip().lower()] = value
            sources.append(mapped)

    text = " ".join([
        _flatten_category_text(product.get("Product name")),
        _flatten_category_text(product.get("About this item")),
        _flatten_category_text(product.get("Website displayed details")),
        _flatten_category_text(product.get("Specifications")),
    ])

    def label_value(*labels):
        wanted = {x.lower() for x in labels}
        for source in sources:
            for label, value in source.items():
                if label in wanted and value not in (None, "", [], {}):
                    return value
        return None

    def pattern_value(pattern):
        match = re.search(pattern, text, re.I)
        return match.group(1).strip(" ,.;") if match else None

    if category == "air_conditioner":
        details = {
            "Product Category": "Air Conditioner",
            "AC Type": label_value("air conditioner type", "ac type", "type", "product type", "system type")
                      or pattern_value(r"\b(window|portable|split|mini[- ]split|ductless|wall[- ]mounted)\s+(?:air conditioner|ac|system)\b"),
            "Installation Type": label_value("installation type", "mounting type")
                                 or pattern_value(r"\b(window[- ]mounted|wall[- ]mounted|through[- ]the[- ]wall|portable|ductless)\b"),
            "Cooling Capacity (BTU)": label_value("cooling capacity", "btu", "btu cooling capacity", "capacity")
                                      or pattern_value(r"\b(\d{1,2}(?:,?\d{3})?)\s*[- ]?BTU\b"),
            "Room Coverage": label_value("room size", "room coverage", "coverage area", "recommended room size", "square footage")
                            or pattern_value(r"\b(?:covers?|cools?|for)\s+(?:up to\s+)?([\d,]+\s*(?:sq\.?\s*ft\.?|square feet))"),
            "Energy Efficiency (SEER/SEER2/EER)": label_value("seer", "seer2", "seer rating", "eer", "energy efficiency ratio")
                                                  or pattern_value(r"\b((?:\d{1,2}(?:\.\d+)?)\s*SEER2?|\d{1,2}(?:\.\d+)?\s*EER)\b"),
            "Voltage": label_value("voltage", "rated voltage", "input voltage", "power supply")
                       or pattern_value(r"\b((?:115|120|208|220|230|240)\s*V(?:olts?)?)\b"),
            "Refrigerant": label_value("refrigerant", "refrigerant type")
                           or pattern_value(r"\b(R[- ]?32|R[- ]?410A|R[- ]?22|R[- ]?454B)\b"),
            "Heating Function": label_value("heating function", "heat pump", "heating")
                                or ("Yes" if re.search(r"\b(heat pump|heating)\b", text, re.I) else None),
            "Wi-Fi / Smart Connectivity": label_value("wi-fi", "wifi", "smart connectivity", "smart features")
                                          or ("Yes" if re.search(r"\b(wi[- ]?fi|wifi|app control|smart control)\b", text, re.I) else None),
            "Remote Control": label_value("remote control", "remote included")
                              or ("Yes" if re.search(r"\bremote control\b", text, re.I) else None),
            "Operating Modes": label_value("operating modes", "modes", "mode"),
            "Fan Speeds": label_value("fan speeds", "number of fan speeds", "fan speed"),
            "Noise Level": label_value("noise level", "sound level", "decibel level")
                           or pattern_value(r"\b(\d{2,3}\s*dB(?:A)?)\b"),
            "Energy Star Certified": label_value("energy star", "energy star certified")
                                     or ("Yes" if re.search(r"\bENERGY STAR\b", text, re.I) else None),
        }
    elif category == "refrigerator":
        details = {
            "Product Category": "Refrigerator",
            "Refrigerator Type": label_value("refrigerator type", "type"),
            "Configuration": label_value("configuration", "door style", "number of doors"),
            "Total Capacity": label_value("total capacity", "capacity"),
            "Freezer Capacity": label_value("freezer capacity"),
            "Refrigerator Capacity": label_value("refrigerator capacity"),
            "Ice / Water Dispenser": label_value("ice maker", "water dispenser", "dispenser"),
            "Defrost Type": label_value("defrost type", "defrost"),
            "Energy Star Certified": label_value("energy star", "energy star certified"),
        }
    elif category == "laptop":
        details = {
            "Product Category": "Laptop",
            "Processor": label_value("processor", "processor model", "cpu"),
            "Processor Generation": label_value("processor generation", "generation"),
            "RAM": label_value("ram", "system memory", "memory"),
            "Storage": label_value("storage", "hard drive capacity", "ssd capacity"),
            "Graphics": label_value("graphics", "graphics card", "gpu"),
            "Screen Size": label_value("screen size", "display size"),
            "Operating System": label_value("operating system", "os"),
            "Battery Life": label_value("battery life"),
        }
    else:
        return {"Product Category": None, "Unique Product Attributes": []}

    return {key: value for key, value in details.items() if value not in (None, "", [], {})}


def visible_model_value(product):
    """Return a model only when Walmart explicitly labels it as a model."""
    labels = product.get("Website displayed details") or {}
    if not isinstance(labels, dict):
        return None
    accepted = {
        "model", "model number", "model no.", "model #", "model name",
        "manufacturer model number", "manufacturer model", "item model number"
    }
    for label, value in labels.items():
        if str(label).strip().lower() in accepted and value not in (None, "", [], {}):
            return value
    return None



# ============================================================
# STANDARDIZED SOCIAL-MENTION JSON NORMALIZER
# ============================================================

# Update these identifiers for each tenant/project deployment.
TENANT_ID = "tenant_001"
PROJECT_ID = "retail_listening_001"
REPORT_SCOPE_IDS = ["TND_Q2_FY27"]
TAXONOMY_VERSION = "TND_2026_V1"
COLLECTION_RUN_ID = None  # generated at runtime when the scraper starts
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_walmart_date(value):
    """Convert Walmart's displayed review date to UTC ISO-8601 where possible."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            stamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()
    for fmt in (
        "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y",
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def normalized_currency(raw, price_display):
    currency = first_value(raw.get("currency"), raw.get("currencyCode"))
    if currency:
        return str(currency).upper()
    text = str(price_display or "")
    if "CA$" in text:
        return "CAD"
    if "$" in text:
        return "USD"
    return None


def normalized_spec_map(product):
    result = {}
    for source in (product.get("Website displayed details"), product.get("Specifications")):
        if isinstance(source, dict):
            for key, value in source.items():
                if value not in (None, "", [], {}):
                    result[str(key).strip().lower()] = value
        elif isinstance(source, list):
            for item in source:
                if not isinstance(item, dict):
                    continue
                label = first_value(item.get("name"), item.get("label"), item.get("key"), item.get("title"))
                value = first_value(item.get("value"), item.get("displayValue"), item.get("values"), item.get("valueName"))
                if label and value not in (None, "", [], {}):
                    result[str(label).strip().lower()] = value
    return result


def spec_lookup(specs, *labels):
    for label in labels:
        value = specs.get(label.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def taxonomy_for_product(product):
    category = detect_product_category(product)
    category_map = {
        "air_conditioner": ("Tech & Durables", "Cooling", "Air Conditioner"),
        "refrigerator": ("Tech & Durables", "Large White Goods", "Refrigerator"),
        "laptop": ("Tech & Durables", "Computing", "Laptop"),
    }
    industry, subcategory, product_type = category_map.get(
        category, ("Retail", None, None)
    )
    return {
        "industry": industry,
        "category": subcategory,
        "subcategory": product_type,
        "product_type": product_type,
        "taxonomy_version": TAXONOMY_VERSION,
        "mapping_method": "rule_based_from_source_category",
        "mapping_status": "mapped" if product_type else "unmapped",
    }


def product_attributes_for_record(product, specs):
    model = first_value(
        find_first_key(product.get("_raw_detail_product") or {}, [
            "model", "modelNumber", "modelNo", "modelName",
            "manufacturerModelNumber", "manufacturerModelNo", "itemModelNumber"
        ]),
        find_first_key(product.get("_raw_product") or {}, [
            "model", "modelNumber", "modelNo", "modelName",
            "manufacturerModelNumber", "manufacturerModelNo", "itemModelNumber"
        ]),
        spec_lookup(specs, "model", "model number", "model no.", "model #",
                    "model name", "manufacturer model number", "item model number"),
        visible_model_value(product),
    )

    category_details = extract_category_specific_details(product)
    capacity_raw = first_value(
        category_details.get("Total Capacity"),
        category_details.get("Cooling Capacity (BTU)"),
        spec_lookup(specs, "capacity", "total capacity", "cooling capacity"),
    )
    capacity = {"value": None, "unit": None}
    if capacity_raw is not None:
        match = re.search(r"([\d,.]+)\s*(lit(?:re|er)s?|l\b|btu|cu\.?\s*ft)", str(capacity_raw), re.I)
        if match:
            capacity = {
                "value": to_number(match.group(1)),
                "unit": match.group(2).lower().replace("litres", "litre").replace("liters", "litre"),
            }

    color = spec_lookup(specs, "color", "colour", "finish")
    energy_rating = spec_lookup(specs, "energy rating", "energy efficiency rating", "energy star")
    return {
        "model_number": model,
        "capacity": capacity,
        "color": color,
        "energy_rating": energy_rating,
        "specifications": {
            **{str(k): v for k, v in specs.items()},
            **{str(k): v for k, v in category_details.items() if k != "Product Category"},
        },
    }


def normalize_review_record(product, review, review_index, collected_at):
    """Create one standardized social_mention document for one Walmart review."""
    raw = product.get("_raw_product") or {}
    raw_detail = product.get("_raw_detail_product") or {}
    specs = normalized_spec_map(product)
    product_id = str(product.get("Product ID") or "")
    review_id = first_value(review.get("Review ID"), f"{product_id}_review_{review_index}")
    review_text = first_value(review.get("Review text"), review.get("Review title"))
    title = review.get("Review title")
    review_date = parse_walmart_date(review.get("Review date"))
    product_url = product.get("Product URL")
    price_display = product.get("Price")
    current_price = parse_price_number(price_display)
    currency = normalized_currency(raw_detail or raw, price_display)
    brand_name = first_value(
        product.get("Brand"),
        raw_detail.get("brand"),
        raw_detail.get("brandName"),
    )
    retailer = {
        "retailer_id": None,
        "retailer_name": "Walmart",
    }

    media = []
    for image_url in review.get("Review photos") or []:
        media.append({"media_type": "image", "url": image_url})
    for video_url in review.get("Review videos") or []:
        media.append({"media_type": "video", "url": video_url})

    identity_text = str(review_id)
    content_hash = hashlib.sha256(
        (str(review_text or "") + "|" + str(review_date or "") + "|" + product_id).encode("utf-8")
    ).hexdigest()
    record_id = f"{TENANT_ID}:walmart:{identity_text}"

    rating = to_number(review.get("Rating"))
    original_price = first_value(
        spec_lookup(specs, "was price", "list price", "original price"),
        raw.get("wasPrice"),
        raw_detail.get("wasPrice"),
    )
    original_price_number = parse_price_number(original_price)
    discount_amount = (
        round(original_price_number - current_price, 2)
        if original_price_number is not None and current_price is not None
        and original_price_number >= current_price else None
    )
    discount_percentage = (
        round(discount_amount / original_price_number * 100, 2)
        if discount_amount is not None and original_price_number else None
    )

    verified = review.get("Verified purchase")
    if verified is None:
        verified = None

    source_author_id = first_value(
        review.get("Reviewer ID"), review.get("Reviewer id"), review.get("User ID")
    )
    author_hash = (
        hashlib.sha256(str(source_author_id).encode("utf-8")).hexdigest()
        if source_author_id else None
    )

    return {
        "_id": record_id,
        "schema_version": "1.0",
        "record_type": "social_mention",
        "tenant": {
            "tenant_id": TENANT_ID,
            "project_id": PROJECT_ID,
            "report_scope_ids": REPORT_SCOPE_IDS,
        },
        "source": {
            "platform": "walmart",
            "channel": "ecommerce_review",
            "source_type": "review",
            "source_item_id": str(review_id),
            "source_url": product_url,
            "parent_item_id": product_id or None,
            "published_at": review_date,
            "collected_at": collected_at,
            "updated_at": None,
            "country_code": "US",
            "marketplace": "walmart.com",
        },
        "content": {
            "title": title,
            "text": review_text,
            "language": None,
            "original_language": None,
            "content_type": "review",
            "media": media,
            "hashtags": [],
            "mentions": [],
            "keywords": [],
        },
        "entities": {
            "brands": [
                {
                    "brand_id": None,
                    "brand_name": brand_name,
                    "brand_role": "product_brand",
                }
            ] if brand_name else [],
            "products": [
                {
                    "product_id": product_id or None,
                    "product_name": first_value(product.get("Product name"), raw.get("name"), raw.get("title")),
                    "sku": None,
                    "model_number": product_attributes_for_record(product, specs)["model_number"],
                    "product_url": product_url,
                }
            ],
            "retailer": retailer,
            "other_brands": [],
        },
        "taxonomy": taxonomy_for_product(product),
        "product_attributes": product_attributes_for_record(product, specs),
        "commercial": {
            "price": {
                "listed_price": {
                    "amount": original_price_number,
                    "currency": currency,
                },
                "selling_price": {
                    "amount": current_price,
                    "currency": currency,
                },
                "original_price": {
                    "amount": original_price_number,
                    "currency": currency,
                },
                "discount_amount": discount_amount,
                "discount_percentage": discount_percentage,
                "price_type": "promotional" if discount_amount is not None else "regular",
                "price_observed_at": collected_at,
            },
            "price_range": {
                "minimum": None,
                "maximum": None,
                "currency": currency,
                "period_start": None,
                "period_end": None,
            },
            "promotion": {
                "is_promotional": discount_amount is not None,
                "promotion_type": None,
                "promotion_name": None,
                "coupon_available": None,
                "coupon_code": None,
            },
            "payment_options": {
                "emi_available": None,
                "no_cost_emi": None,
                "bank_offer_available": None,
                "exchange_offer_available": None,
                "cash_on_delivery": None,
            },
            "availability": {
                "status": first_value(raw.get("availabilityStatus"), raw.get("availability")),
                "stock_quantity": None,
            },
            "delivery": {
                "delivery_charge": {"amount": None, "currency": currency},
                "estimated_delivery_date": None,
                "delivery_type": None,
            },
            "warranty": {
                "duration_months": None,
                "warranty_type": spec_lookup(specs, "warranty type", "warranty"),
            },
        },
        "metrics": {
            "rating": {
                "value": rating,
                "scale_min": 1,
                "scale_max": 5,
            },
            "engagement": {
                "likes": None,
                "comments": None,
                "shares": None,
                "views": None,
                "reactions": None,
            },
            "helpful_votes": to_number(review.get("Helpful votes")),
            "review_count": to_number(
                (product.get("_rating_summary") or {}).get("Number of Reviews")
            ),
            "author_followers": None,
            "reach": None,
        },
        "author": {
            "source_author_id_hash": author_hash,
            "display_name": review.get("Reviewer name"),
            "is_verified_purchase": verified,
            "profile_url": None,
            "follower_count": None,
        },
        "location": {
            "country_code": "US",
            "region": None,
            "city": None,
            "location_precision": "country",
        },
        "campaign": {
            "campaign_name": None,
            "campaign_type": None,
            "event_name": None,
            "campaign_id": None,
        },
        "deduplication": {
            "content_hash": f"sha256:{content_hash}",
            "duplicate_status": "unique",
            "canonical_record_id": record_id,
            "duplicate_of": None,
        },
        "provenance": {
            "collection_run_id": COLLECTION_RUN_ID,
            "connector_name": "walmart_playwright_scraper",
            "connector_version": CONNECTOR_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "source_payload_ref": None,
            "ingestion_timestamp": collected_at,
        },
        "quality": {
            "status": "accepted" if review_text or rating is not None else "partial",
            "is_valid": bool(product_id and review_id),
            "is_relevant": True,
            "validation_errors": [],
            "missing_optional_fields": [
                key for key, value in {
                    "review_text": review_text,
                    "rating": rating,
                    "published_at": review_date,
                    "brand_name": brand_name,
                    "model_number": product_attributes_for_record(product, specs)["model_number"],
                }.items() if value is None
            ],
        },
        "lifecycle": {
            "first_seen_at": collected_at,
            "last_seen_at": collected_at,
            "record_status": "active",
        },
    }


def make_standardized_records(products):
    collected_at = utc_now_iso()
    records = []
    for product in products:
        reviews = product.get("Customer reviews") or []
        for index, review in enumerate(reviews, start=1):
            records.append(normalize_review_record(product, review, index, collected_at))
    return records


# ============================================================
# OUTPUT
# ============================================================

def clean_product(product):
    return {key: value for key, value in product.items() if not key.startswith("_")}


def save_json(products):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    standardized_records = make_standardized_records(products)

    # The product JSON is the standardized social-mention collection.
    with open(PRODUCT_JSON, "w", encoding="utf-8") as file:
        json.dump(standardized_records, file, indent=2, ensure_ascii=False)

    # Keep the review JSON for compatibility; it contains the same normalized records.
    with open(REVIEWS_JSON, "w", encoding="utf-8") as file:
        json.dump(standardized_records, file, indent=2, ensure_ascii=False)


def save_csv(products):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    product_fields = [
        "Search query", "Position", "Product ID", "Product name", "Brand",
        "Price", "Product rating", "Number of ratings", "Product URL",
        "Product images", "Product videos", "About this item",
        "Specifications", "Website displayed details", "Customer reviews",
    ]

    with open(PRODUCT_CSV, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=product_fields)
        writer.writeheader()
        for product in products:
            clean = clean_product(product)
            writer.writerow({
                key: (
                    json.dumps(clean.get(key), ensure_ascii=False)
                    if isinstance(clean.get(key), (list, dict))
                    else clean.get(key)
                )
                for key in product_fields
            })

    review_fields = [
        "Product ID", "Product name", "Product URL", "Review ID",
        "Reviewer name", "Rating", "Review title", "Review text",
        "Review date", "Verified purchase", "Review badges",
        "Helpful votes", "Not helpful votes", "Review photos",
    ]

    with open(REVIEWS_CSV, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=review_fields)
        writer.writeheader()

        for product in products:
            for review in product.get("Customer reviews", []):
                row = {
                    "Product ID": product.get("Product ID"),
                    "Product name": product.get("Product name"),
                    "Product URL": product.get("Product URL"),
                    **review,
                }
                writer.writerow({
                    key: (
                        json.dumps(row.get(key), ensure_ascii=False)
                        if isinstance(row.get(key), (list, dict))
                        else row.get(key)
                    )
                    for key in review_fields
                })


# ============================================================
# MAIN SCRAPER
# ============================================================

def scrape_walmart():
    global COLLECTION_RUN_ID
    COLLECTION_RUN_ID = "RUN_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    all_products = []
    seen_product_ids = set()

    with sync_playwright() as playwright:
        session = BrowserSession(playwright)
        session.start()

        try:
            for query in SEARCH_QUERIES:
                print("\n" + "=" * 70)
                print("SEARCH CATEGORY:", query)
                print("=" * 70)

                query_products = []

                for page_number in range(1, MAX_SEARCH_PAGES + 1):
                    search_url = make_search_url(query, page_number)
                    props = get_page_props(session.page, search_url)

                    items = get_search_items(props) if props else []

                    if items:
                        for item in items:
                            product_id = str(first_value(
                                item.get("usItemId"), item.get("id")
                            ))
                            if not product_id or product_id in seen_product_ids:
                                continue

                            seen_product_ids.add(product_id)
                            product = product_from_search_item(
                                item,
                                position=len(query_products) + 1,
                                query=query,
                            )
                            query_products.append(product)

                    else:
                        # Use visible product cards if embedded page JSON is missing.
                        cards = get_dom_product_cards(session.page)
                        for card in cards:
                            product_id = str(card.get("id") or "")
                            if not product_id or product_id in seen_product_ids:
                                continue
                            seen_product_ids.add(product_id)
                            query_products.append(
                                product_from_dom_card(
                                    card,
                                    position=len(query_products) + 1,
                                    query=query,
                                )
                            )

                    print(
                        f"Search page {page_number}: "
                        f"{len(query_products)} products collected for {query}"
                    )

                    if len(query_products) >= PRODUCTS_PER_QUERY:
                        break
                    if not items and page_number > 1:
                        break

                    session.page.wait_for_timeout(DELAY_BETWEEN_PAGES_MS)

                query_products = query_products[:PRODUCTS_PER_QUERY]
                all_products.extend(query_products)

                # Save listing data before opening individual product pages.
                save_json(all_products)
                save_csv(all_products)

            # Product details and customer reviews.
            if FETCH_PRODUCT_DETAILS or SCRAPE_REVIEWS:
                for index, product in enumerate(all_products, start=1):
                    print("\n" + "-" * 70)
                    print(
                        f"PRODUCT {index}/{len(all_products)}: "
                        f"{product.get('Product name')}"
                    )
                    print("-" * 70)

                    try:
                        if FETCH_PRODUCT_DETAILS:
                            enrich_product(session.page, product)

                        if SCRAPE_REVIEWS:
                            scrape_product_reviews(session.page, product)


                    except Exception as exc:
                        print(
                            f"Could not fully enrich product "
                            f"{product.get('Product ID')}: {exc}"
                        )

                    # Save progress after each product.
                    save_json(all_products)
                    save_csv(all_products)
                    session.page.wait_for_timeout(DELAY_BETWEEN_PRODUCTS_MS)

        finally:
            save_json(all_products)
            save_csv(all_products)
            session.close()

    return all_products


def main():
    global SEARCH_QUERIES, PRODUCTS_PER_QUERY, MAX_SEARCH_PAGES, MAX_REVIEW_PAGES

    print("=" * 65)
    print("WALMART DYNAMIC PRODUCT SCRAPER")
    print("=" * 65)
    print("Enter one or more search terms/categories.")
    print("Examples: laptops, refrigerators, air conditioners, smartphones")
    print("Separate multiple terms with commas.")

    raw_queries = input("\nEnter product/category search terms: ").strip()
    SEARCH_QUERIES = unique(
        [term.strip() for term in raw_queries.split(",") if term.strip()]
    )
    if not SEARCH_QUERIES:
        print("No search term entered. Exiting.")
        return

    def ask_int(prompt, default, minimum=1):
        value = input(f"{prompt} [{default}]: ").strip()
        if not value:
            return default
        try:
            return max(minimum, int(value))
        except ValueError:
            print(f"Invalid number. Using default: {default}")
            return default

    PRODUCTS_PER_QUERY = ask_int("Maximum products per search term", 40)
    MAX_SEARCH_PAGES = ask_int("Maximum search pages per term", 2)

    review_limit = input(
        "Maximum review pages per product (enter 'all' for all available) [5]: "
    ).strip().lower()
    if review_limit == "all":
        MAX_REVIEW_PAGES = None
    elif review_limit:
        try:
            MAX_REVIEW_PAGES = max(1, int(review_limit))
        except ValueError:
            MAX_REVIEW_PAGES = 5

    print("\nSearch terms:", ", ".join(SEARCH_QUERIES))
    print("Products per term:", PRODUCTS_PER_QUERY)
    print("Search pages per term:", MAX_SEARCH_PAGES)
    print("Review page limit:", "all available" if MAX_REVIEW_PAGES is None else MAX_REVIEW_PAGES)
    print("Output folder:", OUTPUT_DIR)

    products = scrape_walmart()
    review_count = sum(len(p.get("Customer reviews", [])) for p in products)

    print("\n" + "=" * 60)
    print("SCRAPING FINISHED")
    print("=" * 60)
    print("Products collected:", len(products))
    print("Reviews collected :", review_count)
    print("Master JSON       :", PRODUCT_JSON)
    print("Review JSON       :", REVIEWS_JSON)
    print("Product CSV       :", PRODUCT_CSV)
    print("Review CSV        :", REVIEWS_CSV)


if __name__ == "__main__":
    main()
