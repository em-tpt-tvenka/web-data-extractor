"""
Flipkart product + review scraper (Python + Playwright, sync API)
Output = ONE product record in the "social_mention" schema (schema_version 1.0),
with all reviews/comments inside it as a `reviews` array

Usage:
    pip install playwright
    playwright install chromium

    python flipkart_scraper.py                         # asks for the product link
    python flipkart_scraper.py "<product url>"
    python flipkart_scraper.py "<url>" --headed        # watch the browser
    python flipkart_scraper.py "<url>" --sort helpful      # helpful | recent | positive | negative
    python flipkart_scraper.py "<url>" --debug             # saves page text when scraping stops early

    # schema / tenant options
    python flipkart_scraper.py "<url>" \
        --tenant-id tenant_001 --project-id retail_listening_001 \
        --report-scope-ids TND_Q2_FY27 \
        --client-brand "Brand A" \
        --industry "Tech & Durables" --category "Large White Goods" \
        --subcategory "Refrigerator" --product-type "Frost Free" \
        --format json            # json (single object, default) | jsonl (one line)
        --save-raw               # also keep the old raw scrape output

Output: flipkart_<product_id>.json   (one record; reviews = array inside it)
"""

import argparse
import calendar
import hashlib
import json
import random
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, urlencode

from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SCHEMA_VERSION = "1.0"
CONNECTOR_NAME = "flipkart_connector"
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"

SORT_PARAM = {
    "helpful": None,  # Flipkart default = Most Helpful
    "recent": "MOST_RECENT",
    "positive": "POSITIVE_FIRST",
    "negative": "NEGATIVE_FIRST",
}
SORT_CHIP = {"recent": "Latest", "positive": "Positive", "negative": "Negative"}

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def get_ids(url: str) -> dict:
    qs = parse_qs(urlparse(url).query)
    m = re.search(r"/p/(itm[0-9a-z]+)", url, re.I)
    return {
        "product_id": (qs.get("pid") or [None])[0],
        "item_id": m.group(1) if m else None,
        "listing_id": (qs.get("lid") or [None])[0],
    }


def to_number(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.]", "", str(s))
    if not s:
        return None
    return float(s) if "." in s else int(s)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def section(text: str, start: str, ends=()) -> str:
    m = re.search(start, text, re.I | re.M)
    if not m:
        return ""
    rest = text[m.end():]
    cut = len(rest)
    for e in ends:
        n = re.search(e, rest, re.I | re.M)
        if n:
            cut = min(cut, n.start())
    return rest[:cut].strip()


def group_offers(block: str, start_regex: str) -> list:
    skip = {"best value for you", "apply", "know more", "t&c", ""}
    offers, cur = [], []
    for line in (l.strip() for l in block.splitlines()):
        if line.lower() in skip:
            continue
        if re.search(start_regex, line, re.I) and cur:
            offers.append(" | ".join(cur))
            cur = []
        cur.append(line)
    if cur:
        offers.append(" | ".join(cur))
    return offers


def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        print(f"  [warn] {e}", file=sys.stderr)
        return default


def weighted_average(breakdown: dict):
    total = sum(breakdown.values())
    if not total:
        return None
    return round(sum(int(k[0]) * v for k, v in breakdown.items()) / total, 2)


# --------------------------------------------------------------------------- #
# Date helpers  ("5 months ago"  ->  ISO date)
# --------------------------------------------------------------------------- #

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
REL_RE = re.compile(r"\b(?:(\d+)|an?)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.I)


def sub_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) - n
    y, m = divmod(idx, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def parse_posted(text: str):
    """Return (relative_text, iso_date) from strings like
    '5 months ago', '1 year ago', '3 days ago', 'Jun, 2024'."""
    if not text:
        return None, None
    today = date.today()
    m = REL_RE.search(text)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2).lower()
        if unit in ("second", "minute", "hour"):
            d = today
        elif unit == "day":
            d = today - timedelta(days=n)
        elif unit == "week":
            d = today - timedelta(weeks=n)
        elif unit == "month":
            d = sub_months(today, n)
        else:
            d = sub_months(today, 12 * n)
        return clean(m.group(0)), d.isoformat()
    if re.search(r"\byesterday\b", text, re.I):
        return "yesterday", (today - timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b|just now", text, re.I):
        return "today", today.isoformat()
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})[,\s]+(\d{4})", text)
    if m and m.group(2)[:3].lower() in MONTHS:
        try:
            return clean(m.group(0)), date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1))).isoformat()
        except ValueError:
            pass
    m = re.search(r"([A-Za-z]{3,9})[,\s]+(\d{4})", text)
    if m and m.group(1)[:3].lower() in MONTHS:
        return clean(m.group(0)), date(int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1).isoformat()
    return None, None


# --------------------------------------------------------------------------- #
# Browser actions
# --------------------------------------------------------------------------- #


def body_len(page):
    return page.evaluate("document.body.innerText.length")


def expand_sections(page):
    patterns = [
        r"^Product highlights$",
        r"^Specifications$",
        r"^Read More$",
        r"^\+\s*\d+\s*more offers?$",
        r"^View all (offers|specifications)$",
        r"^Show more$",
        r"^Manufacturing,?\s*Packaging\s*(and|&)\s*Import Info$",
        r"^Manufacturer (Info|Details)$",
    ]
    for p in patterns:
        loc = page.get_by_text(re.compile(p, re.I))
        count = min(loc.count(), 3)
        for i in range(count):
            try:
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                before = body_len(page)
                el.scroll_into_view_if_needed(timeout=1500)
                el.click(timeout=1500)
                page.wait_for_timeout(400)
                if body_len(page) < before:
                    el.click(timeout=1500)
                    page.wait_for_timeout(300)
            except Exception:
                pass


def scroll_page(page):
    h = page.evaluate("document.body.scrollHeight")
    y = 0
    while y < h:
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(200)
        y += 900
        h = page.evaluate("document.body.scrollHeight")
    page.evaluate("window.scrollTo(0, 0)")


# --------------------------------------------------------------------------- #
# In-page extractors (JavaScript)
# --------------------------------------------------------------------------- #

JS_JSONLD = """
() => [...document.querySelectorAll('script[type="application/ld+json"]')]
        .map(s => { try { return JSON.parse(s.textContent) } catch(e) { return null } })
        .filter(Boolean)
"""

JS_PRICES = r"""
() => {
  const out = {selling: null, mrp: null, discount: null};
  const top = e => e.getBoundingClientRect().top + window.scrollY;
  const struck = e => {
    for (let n = e, i = 0; n && i < 4; n = n.parentElement, i++) {
      const cs = getComputedStyle(n);
      if ((cs.textDecorationLine || cs.textDecoration || '').includes('line-through')) return true;
      if (['DEL', 'S', 'STRIKE'].includes(n.tagName)) return true;
    }
    return false;
  };
  const all = [...document.querySelectorAll('div,span,del,s,p')].filter(e => top(e) < 1800);
  let best = 0;
  for (const e of all.filter(e => e.children.length === 0)) {
    const t = e.textContent.trim();
    if (/^₹\s?[\d,]{3,}$/.test(t)) {
      if (struck(e)) { if (!out.mrp) out.mrp = t; }
      else {
        const fs = parseFloat(getComputedStyle(e).fontSize);
        if (fs > best) { best = fs; out.selling = t; }
      }
    }
  }
  for (const e of all) {
    const t = (e.innerText || '').trim();
    if (t.length > 14) continue;
    const d = t.match(/^[^\d]{0,3}\s?(\d{1,2})\s?%\s*(off)?$/i);
    if (d) { out.discount = d[1] + '%'; break; }
  }
  return out;
}
"""

# PRODUCT GALLERY ONLY (left column at the top of the page).
# Ignored: everything below the first section heading (From the Manufacturer,
# Description, Specifications, Ratings & Reviews ...), customer review photos,
# similar-product images and anything in the right-hand column.
JS_IMAGES_VIDEOS = r"""
() => {
  const W = window.innerWidth;
  const top = e => e.getBoundingClientRect().top + window.scrollY;
  const stop = /^(from the manufacturer|product description|description|ratings & reviews|specifications|questions and answers|similar products|all details)$/i;
  let cutoff = 1500;
  [...document.querySelectorAll('div,span,h1,h2,h3')]
    .filter(e => e.children.length === 0 && stop.test(e.textContent.trim()))
    .forEach(e => { const y = top(e); if (y > 450 && y < cutoff) cutoff = y; });
  const inGallery = e => {
    const r = e.getBoundingClientRect();
    return r.width >= 40 && r.height >= 40 && (r.left + r.width / 2) < W * 0.5 && (r.top + window.scrollY) < cutoff;
  };
  const imgs = new Set();
  const add = u => {
    if (!u || !/rukminim/.test(u)) return;
    const m = u.match(/\/image\/(\d+)\/(\d+)\//);
    if (!m || parseInt(m[1]) < 100) return;
    imgs.add(u.replace(/\/image\/\d+\/\d+\//, '/image/832/832/').split('?')[0]);
  };
  document.querySelectorAll('img').forEach(i => {
    if (!inGallery(i)) return;
    add(i.currentSrc || i.src); add(i.getAttribute('data-src'));
  });
  document.querySelectorAll('div,span,a').forEach(e => {
    if (!inGallery(e)) return;
    const bg = getComputedStyle(e).backgroundImage;
    const m = bg && bg.match(/url\("?([^")]+)"?\)/);
    if (m) add(m[1]);
  });
  const vids = new Set();
  const addV = u => { if (u && /^https?:/.test(u)) vids.add(u); };
  document.querySelectorAll('video').forEach(v => {
    if (!inGallery(v)) return;
    addV(v.currentSrc || v.src);
    v.querySelectorAll('source').forEach(s => addV(s.src));
  });
  document.querySelectorAll('iframe').forEach(f => {
    if (inGallery(f) && /youtube|vimeo|video/.test(f.src)) addV(f.src);
  });
  document.querySelectorAll('a[href*="youtube.com"], a[href*="youtu.be"]').forEach(a => {
    if (inGallery(a)) addV(a.href);
  });
  return {images: [...imgs], videos: [...vids]};
}
"""

# small thumbnails of the gallery strip (viewport coordinates, page scrolled to top)
JS_GALLERY_THUMBS = r"""
() => {
  const W = window.innerWidth, H = window.innerHeight;
  const out = [];
  document.querySelectorAll('img').forEach(i => {
    const r = i.getBoundingClientRect();
    const u = i.currentSrc || i.src || '';
    if (/rukminim/.test(u) && r.width >= 30 && r.width <= 130 && r.height >= 30 && r.height <= 130
        && r.left < W * 0.18 && r.top > 80 && r.top < H - 20) {
      out.push({x: r.left + r.width / 2, y: r.top + r.height / 2});
    }
  });
  return out;
}
"""

JS_HIGHLIGHTS = """
() => {
  const heads = [...document.querySelectorAll('div,span,h2,h3')]
      .filter(e => e.children.length === 0 && /^Product highlights$/i.test(e.textContent.trim()));
  for (const h of heads) {
    let node = h;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const lis = node.querySelectorAll('li');
      if (lis.length) return [...lis].map(l => l.innerText.trim()).filter(Boolean);
    }
  }
  return [];
}
"""

JS_SPECS_TABLES = """
() => {
  const specs = {};
  document.querySelectorAll('table').forEach(t => {
    const rows = [...t.querySelectorAll('tr')]
        .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))
        .filter(r => r.length >= 2 && r[0]);
    if (!rows.length) return;
    let title = 'General';
    const p = t.previousElementSibling || (t.parentElement && t.parentElement.previousElementSibling);
    if (p && p.innerText) title = p.innerText.trim().split('\\n')[0] || title;
    specs[title] = Object.assign(specs[title] || {}, Object.fromEntries(rows.map(r => [r[0], r.slice(1).join(' ')])));
  });
  return specs;
}
"""

JS_SPECS_TEXT = """
() => {
  const heads = [...document.querySelectorAll('div,span,h2,h3')]
      .filter(e => e.children.length === 0 && /^Specifications$/i.test(e.textContent.trim()));
  for (const h of heads) {
    let node = h;
    for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
      if (node.innerText && node.innerText.length > 400) return node.innerText;
    }
  }
  return '';
}
"""

JS_VARIANTS = """
() => {
  const labels = ['Color','Colour','Storage','RAM','Size','Variant','Pack of','Capacity','Model'];
  const out = {};
  const leaves = [...document.querySelectorAll('div,span')].filter(e => e.children.length === 0);
  for (const label of labels) {
    const el = leaves.find(e => new RegExp('^' + label + '\\\\s*:?$', 'i').test(e.textContent.trim()));
    if (!el) continue;
    let node = el.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      const links = [...node.querySelectorAll('a[href*="/p/itm"]')];
      if (links.length) {
        out[label] = links.map(a => ({
          option: (a.innerText || a.getAttribute('title') || a.querySelector('img')?.alt || '').trim().replace(/\\n+/g, ' '),
          url: a.href,
          product_id: (a.href.match(/[?&]pid=([^&]+)/) || [])[1] || null
        }));
        break;
      }
    }
  }
  return out;
}
"""

JS_CLICK_MORE = """
() => {
  let n = 0;
  document.querySelectorAll('span,a,div,button').forEach(e => {
    if (e.children.length === 0 && /^(more|read more)$/i.test(e.textContent.trim())) {
      const a = e.closest('a');
      const href = a && a.getAttribute('href');
      if (href && !href.startsWith('#') && !href.startsWith('javascript')) return;
      e.click(); n++;
    }
  });
  return n;
}
"""

JS_ASPECT_CHIPS = """
() => {
  const leaf = [...document.querySelectorAll('div,span,button,a')]
      .find(e => e.children.length === 0 && /^Overall$/i.test(e.textContent.trim()));
  if (!leaf) return [];
  let node = leaf;
  for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
    const p = node.parentElement;
    if (p && p.children.length >= 3) {
      return [...p.children].map(c => c.innerText.trim()).filter(Boolean);
    }
  }
  return [];
}
"""

# --------------------------------------------------------------------------- #
# Specifications  ->  clean, analysis-friendly structure
# --------------------------------------------------------------------------- #

SPEC_HEADERS = {
    "general", "dimensions", "warranty", "connectivity", "display", "camera",
    "battery", "processor", "multimedia", "sound", "other", "in the box",
    "network & connectivity", "memory & storage", "os & processor",
    "performance", "design", "platform", "storage", "ports", "software",
    "sensors", "features", "audio", "video",
}
SPEC_NOISE = {
    "specifications", "read more", "read less", "view more", "view less",
    "show more", "show less", "all details", "know more",
}


def is_spec_header(line: str) -> bool:
    l = line.strip().lower()
    if l in SPEC_HEADERS:
        return True
    return bool(re.fullmatch(r"[a-z0-9 &/\-]+ features", l))


def parse_spec_text(block: str) -> dict:
    lines = [clean(l) for l in block.splitlines()]
    lines = [l for l in lines if l and l.lower() not in SPEC_NOISE]

    sections, current, buf = [], "General", []
    for l in lines:
        if is_spec_header(l):
            sections.append((current, buf))
            current, buf = l.title(), []
        else:
            buf.append(l)
    sections.append((current, buf))

    specs = {}
    for name, items in sections:
        rows = {}
        i = 0
        while i < len(items):
            line = items[i]
            if ":" in line and not line.endswith(":"):
                k, _, v = line.partition(":")
                rows[k.strip()] = v.strip()
                i += 1
            elif i + 1 < len(items):
                rows[line] = items[i + 1]
                i += 2
            else:
                rows[line] = ""
                i += 1
        if rows:
            specs.setdefault(name, {}).update(rows)
    return specs


def build_specs(page, text: str) -> dict:
    specs = safe(lambda: page.evaluate(JS_SPECS_TABLES), {}) or {}
    source = "table"
    if not specs:
        block = safe(lambda: page.evaluate(JS_SPECS_TEXT), "") or ""
        if not block:
            block = section(
                text, r"^Specifications\s*$",
                [r"^Ratings & Reviews", r"^Questions and Answers", r"^Safe and Secure"])
        specs = parse_spec_text(block)
        source = "text"

    table = [{"section": sec, "key": k, "value": v}
             for sec, rows in specs.items() for k, v in rows.items()]
    flat = {f"{sec} | {k}": v for sec, rows in specs.items() for k, v in rows.items()}
    return {
        "specifications": specs,
        "specifications_table": table,
        "specifications_flat": flat,
        "specifications_parsed_from": source,
    }


# --------------------------------------------------------------------------- #
# Reviews
# --------------------------------------------------------------------------- #


def parse_breakdown(text: str) -> dict:
    found = re.findall(r"^([1-5])\s*★\s*(?:\n\s*)?([\d,]+)\s*$", text, re.M)
    return {f"{s}_star": to_number(c) for s, c in found}


def parse_feedback_percent(head: str):
    pos = re.search(r"(\d{1,3})\s*%\s*positive\s+feedback", head, re.I | re.S)
    neg = re.search(r"(\d{1,3})\s*%\s*negative\s+feedback", head, re.I | re.S)
    return (int(pos.group(1)) if pos else None, int(neg.group(1)) if neg else None)


def parse_review_summary(text: str) -> dict:
    head = text.split("User reviews sorted by")[0]
    m = re.search(r"([\d,]+)\s+ratings?\s+and\s+([\d,]+)\s+reviews?", head, re.I | re.S)
    breakdown = parse_breakdown(head)
    pos, neg = parse_feedback_percent(head)
    return {
        "total_ratings": to_number(m.group(1)) if m else (sum(breakdown.values()) or None),
        "total_reviews": to_number(m.group(2)) if m else None,
        "average_rating": weighted_average(breakdown),
        "rating_breakdown": breakdown,
        "positive_feedback_percent": pos,
        "negative_feedback_percent": neg,
    }


def split_reviewer(lines):
    """lines = the lines just before 'Helpful ...'. Flipkart prints the name and
    ', Location' either on one line or on two separate lines.
    Returns (name, location, number_of_lines_used)."""
    if not lines:
        return "", "", 0
    last = lines[-1]
    if last.startswith(",") and len(lines) >= 2:
        return clean(lines[-2]), clean(last.lstrip(", ")), 2
    if len(lines) >= 2 and lines[-2].rstrip().endswith(",") and "," not in last:
        return clean(lines[-2].rstrip(", ")), clean(last), 2
    if "," in last:
        n, _, loc = last.partition(",")
        return clean(n), clean(loc), 1
    return clean(last), "", 1


def parse_reviews(text: str) -> list:
    """Parse the visible text of a Flipkart reviews page into review dicts."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1) where does each review start?  ("5.0 • Terrific")
    starts, i = [], 0
    while i < len(lines):
        m = re.match(r"^([1-5](?:\.\d)?)\s*[•·]\s*(.*)$", lines[i])
        if m:
            starts.append((i, float(m.group(1)), m.group(2).strip(), 1))
            i += 1
            continue
        if re.fullmatch(r"[1-5](?:\.\d)?", lines[i]) and i + 1 < len(lines) and lines[i + 1] in ("•", "·"):
            title = lines[i + 2] if i + 2 < len(lines) else ""
            if title.lower().startswith("review for"):
                title, used = "", 2
            else:
                used = 3
            starts.append((i, float(lines[i]), title, used))
            i += used
            continue
        i += 1

    reviews = []
    for n, (idx, rating, title, used) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = lines[idx + used:end]

        # --- "Verified Purchase" / "Certified Buyer" line + the date that follows
        v_idx = next((k for k, l in enumerate(block)
                      if re.search(r"verified purchase|certified buyer", l, re.I)), None)
        meta_text = ""
        if v_idx is not None:
            meta_text = " ".join(block[v_idx:v_idx + 3])
            cut = v_idx + 1
            for k in (1, 2):  # date can sit on the next line(s): "·", "5 months ago"
                if v_idx + k < len(block) and (
                        REL_RE.search(block[v_idx + k]) or block[v_idx + k] in ("·", "•")
                        or re.search(r"\b(today|yesterday)\b|[A-Za-z]{3,9}[,\s]+\d{4}", block[v_idx + k], re.I)):
                    cut = v_idx + k + 1
            block = block[:cut]
        else:
            tail = " ".join(block[-4:])
            meta_text = tail
        posted_rel, posted_date = parse_posted(meta_text)

        # --- variant line
        variant_raw, variant, var_idx = None, {}, -1
        for k, l in enumerate(block):
            if l.lower().startswith("review for"):
                variant_raw, var_idx = clean(l.split(":", 1)[-1]), k
                for part in re.split(r"\s*[•·]\s*", variant_raw):
                    kk, _, vv = part.strip().partition(" ")
                    if kk and vv:
                        variant[kk] = vv.strip()
                break

        # --- helpful counts
        h_idx = next((k for k, l in enumerate(block) if re.match(r"^Helpful", l, re.I)), None)
        helpful = second = None
        if h_idx is not None:
            hm = re.search(r"(\d[\d,]*)", block[h_idx])
            nums_after = [to_number(l) for l in block[h_idx + 1:h_idx + 4] if re.fullmatch(r"[\d,]+", l)]
            if hm:
                helpful = to_number(hm.group(1))
                second = nums_after[0] if nums_after else None
            else:
                helpful = nums_after[0] if nums_after else None
                second = nums_after[1] if len(nums_after) > 1 else None

        # --- reviewer name + location (lines just before Helpful / Verified)
        pre_end = h_idx if h_idx is not None else (v_idx if v_idx is not None else len(block))
        pre = block[var_idx + 1:pre_end]
        name, location, used_lines = split_reviewer(pre)
        body_lines = pre[:len(pre) - used_lines] if used_lines else pre
        body = clean(" ".join(l for l in body_lines if l.lower() not in ("more", "read more", "...")))

        reviews.append({
            "rating": rating,
            "title": title,
            "review_text": body,
            "variant_reviewed": variant,
            "variant_reviewed_raw": variant_raw,
            "reviewer_name": name,
            "reviewer_location": location,
            "verified_purchase": bool(re.search(r"verified purchase|certified buyer", meta_text, re.I)),
            "posted_relative": posted_rel,      # e.g. "5 months ago"
            "posted_date": posted_date,         # e.g. "2026-06-12"  (approximate for "x months ago")
            "helpful_count": helpful,
            "second_reaction_count": second,
        })
    return reviews


def build_review_url(page_url: str) -> str:
    u = urlparse(page_url)
    qs = parse_qs(u.query)
    path = u.path.replace("/p/", "/product-reviews/")
    q = {k: qs[k][0] for k in ("pid", "lid") if k in qs}
    q["marketplace"] = qs.get("marketplace", ["FLIPKART"])[0]
    return f"{u.scheme}://{u.netloc}{path}?{urlencode(q)}"


def with_page(url: str, p: int) -> str:
    u = urlparse(url)
    qs = {k: v[0] for k, v in parse_qs(u.query).items()}
    qs["page"] = str(p)
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(qs)}"


def load_review_page(page, url, wait=1800):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait)
    page.keyboard.press("Escape")
    scroll_page(page)
    safe(lambda: page.evaluate(JS_CLICK_MORE))
    page.wait_for_timeout(500)


def parse_aspect_head(head: str, chips):
    """Aspect view shows a circle with the rating and
    '93% Positive feedback / 7% Negative feedback'."""
    chip_labels = {clean(c.split("\n")[0]).lower() for c in chips}
    rating = None
    for l in (clean(x) for x in head.splitlines()):
        if l.lower() in chip_labels:
            continue
        if re.fullmatch(r"[1-5](?:\.\d)?", l):
            rating = float(l)
            break
    pos, neg = parse_feedback_percent(head)
    return rating, pos, neg


def scrape_aspects(page) -> dict:
    chips = safe(lambda: page.evaluate(JS_ASPECT_CHIPS), []) or []
    aspects = {}
    for chip in chips:
        label = chip.split("\n")[0].strip()
        name = clean(re.sub(r"[\d.★]+", "", label))
        if not name or name.lower() == "overall":
            continue
        try:
            page.get_by_text(label, exact=True).first.click(timeout=3000)
        except Exception:
            aspects[name] = {"rating": None, "positive_feedback_percent": None, "negative_feedback_percent": None}
            continue
        head = ""
        for _ in range(3):  # wait until the aspect numbers are on screen
            page.wait_for_timeout(1200)
            head = page.inner_text("body").split("User reviews sorted by")[0]
            if re.search(r"positive\s+feedback", head, re.I):
                break
        rating, pos, neg = parse_aspect_head(head, chips)
        aspects[name] = {
            "rating": rating,
            "positive_feedback_percent": pos,
            "negative_feedback_percent": neg,
        }
    return aspects


def apply_sort(page, review_url, sort):
    """Return the URL (without page=) that lists reviews in the wanted order."""
    if sort == "helpful":
        return review_url
    chip = SORT_CHIP[sort]
    try:
        page.get_by_text(chip, exact=True).first.click(timeout=4000)
        page.wait_for_timeout(2000)
        if "sortOrder" in page.url:
            u = urlparse(page.url)
            qs = {k: v[0] for k, v in parse_qs(u.query).items() if k != "page"}
            return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(qs)}"
    except Exception:
        pass
    return f"{review_url}&sortOrder={SORT_PARAM[sort]}"


def looks_sorted_recent(reviews) -> bool:
    ds = [r["posted_date"] for r in reviews if r["posted_date"]]
    return len(ds) >= 3 and all(ds[i] >= ds[i + 1] for i in range(len(ds) - 1))


def scrape_review_section(page, product_url, max_pages, sort, debug):
    review_url = build_review_url(product_url)
    print(f"  Reviews page: {review_url}")
    load_review_page(page, review_url)
    text = page.inner_text("body")

    summary = parse_review_summary(text)
    summary["aspect_ratings"] = safe(lambda: scrape_aspects(page), {})
    summary["review_url"] = review_url

    # fresh load, then choose the sort order
    load_review_page(page, review_url)
    base = apply_sort(page, review_url, sort)
    print(f"  Sort: {sort}  ->  {base}")

    seen, all_reviews = set(), []
    empty_streak, stop_reason = 0, "reached max pages"
    total_expected = summary.get("total_reviews")
    pages_done = 0

    for p in range(1, max_pages + 1):
        url = with_page(base, p)
        load_review_page(page, url)
        page_text = page.inner_text("body")
        if re.search(r"captcha|access denied|are you a human|unusual traffic", page_text, re.I):
            stop_reason = f"blocked / captcha on page {p}"
            break
        got = parse_reviews(page_text)
        if not got:  # one retry with a longer wait
            page.wait_for_timeout(4000)
            page_text = page.inner_text("body")
            got = parse_reviews(page_text)
        pages_done = p

        new = 0
        for r in got:
            key = (r["reviewer_name"], r["rating"], r["title"], r["review_text"][:80],
                   r["posted_relative"], r["helpful_count"])
            if key in seen:
                continue
            seen.add(key)
            all_reviews.append(r)
            new += 1

        print(f"  page {p}: parsed {len(got)}, kept {new} (total {len(all_reviews)}"
              + (f" / ~{total_expected} on site" if total_expected else "") + ")")

        if not got:
            empty_streak += 1
            if debug or p <= 2:
                with open(f"debug_reviews_page_{p}.txt", "w", encoding="utf-8") as f:
                    f.write(page_text)
                safe(lambda: page.screenshot(path=f"debug_reviews_page_{p}.png", full_page=True))
                print(f"  [debug] 0 reviews parsed on page {p}. Saved debug_reviews_page_{p}.txt / .png")
            if empty_streak >= 2:
                stop_reason = f"no reviews found on pages {p - 1} and {p} (end of list, or Flipkart stopped serving more)"
                break
        else:
            empty_streak = 0
        time.sleep(random.uniform(1.0, 2.0))  # be polite

    summary["pages_scraped"] = pages_done
    summary["stop_reason"] = stop_reason
    summary["sort_used"] = sort
    print(f"  Stopped: {stop_reason}")
    return summary, all_reviews


# --------------------------------------------------------------------------- #
# Product media  (gallery images + videos only)
# --------------------------------------------------------------------------- #


def dedupe_images(urls):
    seen, out = set(), []
    for u in urls:
        if not u:
            continue
        u = re.sub(r"/image/\d+/\d+/", "/image/832/832/", u.split("?")[0])
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def collect_media(page):
    """Product gallery images + videos ONLY. Clicks each gallery thumbnail so video
    players / hi-res images load, then reads the gallery area after every click."""
    imgs, vids = [], []

    def grab():
        m = safe(lambda: page.evaluate(JS_IMAGES_VIDEOS), {}) or {}
        imgs.extend(m.get("images", []))
        vids.extend(m.get("videos", []))

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    grab()
    thumbs = safe(lambda: page.evaluate(JS_GALLERY_THUMBS), []) or []
    for t in thumbs[:20]:
        try:
            page.mouse.click(t["x"], t["y"])
            page.wait_for_timeout(450)
            grab()
        except Exception:
            pass
    safe(lambda: page.keyboard.press("Escape"))
    return dedupe_images(imgs), list(dict.fromkeys(vids))


# --------------------------------------------------------------------------- #
# Main scrape  (raw data - unchanged logic)
# --------------------------------------------------------------------------- #


def price_from_text(text: str) -> dict:
    """Fallback when the DOM price scan finds no struck-through MRP: look at the
    rupee amounts printed around the '15% off' / '↓15%' badge."""
    out = {"selling": None, "mrp": None, "discount": None}
    head = text[:8000]
    m = re.search(r"(?:↓\s*(\d{1,2})\s*%)|(?:\b(\d{1,2})\s*%\s*off\b)", head, re.I)
    if not m:
        return out
    out["discount"] = f"{m.group(1) or m.group(2)}%"
    window = head[max(0, m.start() - 160): m.end() + 160]
    window = re.sub(r"(?:Extra\s*)?₹\s*[\d,]+\s*off", " ", window, flags=re.I)  # '₹500 off' is not a price
    nums = [to_number(x) for x in re.findall(r"₹\s*([\d,]{3,})", window)]
    nums = sorted({n for n in nums if n})
    if len(nums) >= 2:
        out["mrp"], out["selling"] = nums[-1], nums[0]
    return out


MFG_LABELS = [
    ("manufactured_by", r"manufactured\s+by|manufacturer(?:\s+(?:name|details|info|address))?|mfd\.?\s+by|mfg\.?\s+by"),
    ("marketed_by", r"marketed\s+by|marketer(?:\s+(?:name|details))?"),
    ("packed_by", r"packed\s+by|packer(?:\s+(?:name|details))?"),
    ("imported_by", r"imported\s+by|importer(?:\s+(?:name|details))?"),
    ("country_of_origin", r"country\s+of\s+origin"),
    ("generic_name", r"generic\s+name"),
    ("net_quantity", r"net\s+quantity"),
    ("manufacturing_date", r"(?:date\s+of\s+)?manufactur(?:e|ing)\s+date|month\s+and\s+year\s+of\s+manufacture"),
    ("customer_care", r"customer\s+care(?:\s+details)?"),
]
MFG_SPEC_KEYS = {
    "manufactured_by": ["Manufacturer", "Manufactured By", "Manufacturer Name", "Manufacturer Details"],
    "marketed_by": ["Marketed By", "Marketer"],
    "packed_by": ["Packed By", "Packer", "Packer Details"],
    "imported_by": ["Imported By", "Importer", "Importer Details"],
    "country_of_origin": ["Country of Origin"],
    "generic_name": ["Generic Name"],
    "net_quantity": ["Net Quantity"],
    "manufacturing_date": ["Manufacturing Date", "Date of Manufacture"],
    "customer_care": ["Customer Care", "Customer Care Details"],
}


def extract_manufacturer(text: str, flat: dict) -> dict:
    """Manufacturer / packer / importer / origin info from the specs table and
    from the page text ('Manufactured By: ...' or label line + value line)."""
    out = {k: None for k, _ in MFG_LABELS}
    for k, names in MFG_SPEC_KEYS.items():
        v = find_spec(flat, *names)
        if v:
            out[k] = clean(v)

    lines = [clean(l) for l in text.splitlines() if l.strip()]
    label_only = re.compile(r"^(?:" + "|".join(rx for _, rx in MFG_LABELS) + r")\s*:?\s*$", re.I)
    for i, l in enumerate(lines):
        for key, rx in MFG_LABELS:
            if out[key]:
                continue
            m = re.fullmatch(r"(?:" + rx + r")\s*[:\-]\s*(.+)", l, re.I)
            if m:
                val, j = m.group(1).strip(), i + 1
                while val.endswith(",") and j < len(lines) and j < i + 3 and not label_only.match(lines[j]):
                    val += " " + lines[j]
                    j += 1
                out[key] = val
                break
            if re.fullmatch(r"(?:" + rx + r")\s*:?", l, re.I) and i + 1 < len(lines):
                val = lines[i + 1]
                if label_only.match(val):
                    continue
                j = i + 2
                while val.endswith(",") and j < len(lines) and j < i + 4 and not label_only.match(lines[j]):
                    val += " " + lines[j]
                    j += 1
                out[key] = val
                break

    raw = section(
        text,
        r"^(?:Manufacturing,?\s*Packaging\s*(?:and|&)\s*Import\s*Info|Manufacturer\s*(?:Info|Details))\s*$",
        [r"^Ratings & Reviews", r"^Questions and Answers", r"^Safe and Secure"])
    out["raw_text"] = raw[:1500] or None
    out["details"] = out["manufactured_by"]
    out["name"] = out["manufactured_by"].split(",")[0].strip() if out["manufactured_by"] else None
    return out


def scrape(url, headed=False, with_reviews=True, max_review_pages=300,
           sort="recent", debug=False) -> dict:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(
            user_agent=UA, locale="en-IN", viewport={"width": 1440, "height": 900}
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        page.keyboard.press("Escape")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # Flipkart never becomes fully idle - expected

        scroll_page(page)
        expand_sections(page)
        page.wait_for_timeout(800)

        text = page.inner_text("body")
        data = {"url": page.url, **get_ids(page.url)}
        data["scraped_at"] = datetime.now(timezone.utc).isoformat()

        ld = safe(lambda: page.evaluate(JS_JSONLD), [])
        flat = []
        for block in ld:
            flat.extend(block.get("@graph", [block]) if isinstance(block, dict) else block)
        prod = next((b for b in flat if isinstance(b, dict) and b.get("@type") == "Product"), {})
        offer = prod.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        agg = prod.get("aggregateRating") or {}

        data["product_name"] = prod.get("name") or safe(lambda: page.locator("h1").first.inner_text())
        brand = prod.get("brand")
        data["brand"] = brand.get("name") if isinstance(brand, dict) else brand
        data["sku"] = prod.get("sku")
        data["description"] = prod.get("description")

        pr = safe(lambda: page.evaluate(JS_PRICES), {}) or {}
        tp = price_from_text(text)
        price_source = "dom" if pr.get("mrp") else ("text" if tp["mrp"] else "none")
        selling = to_number(pr.get("selling")) or to_number(tp["selling"]) or to_number(offer.get("price"))
        mrp = to_number(pr.get("mrp")) or to_number(tp["mrp"])
        if mrp and selling and selling > mrp:  # picked in the wrong order
            selling, mrp = mrp, selling
        if mrp and selling and mrp == selling:
            mrp = None
        discount = pr.get("discount") or tp["discount"]
        if not discount and selling and mrp and mrp > selling:
            discount = f"{round((mrp - selling) / mrp * 100)}%"
        wow = re.search(r"Buy at\s*₹\s*([\d,]+)", text)
        fee = re.search(r"\+\s*₹\s*([\d,]+)\s*Protect Promise Fee", text, re.I)
        data["price"] = {
            "currency": offer.get("priceCurrency", "INR"),
            "selling_price": selling,
            "mrp": mrp,
            "discount_percent": discount,
            "special_deal_price": to_number(wow.group(1)) if wow else None,
            "protect_promise_fee": to_number(fee.group(1)) if fee else None,
            "availability": offer.get("availability"),
        }
        extra = re.search(r"Extra\s*₹\s*([\d,]+)\s*off", text, re.I)
        data["price"]["discount_amount"] = (
            round(mrp - selling, 2) if mrp and selling and mrp > selling else None)
        data["price"]["extra_discount_amount"] = to_number(extra.group(1)) if extra else None
        data["price"]["price_source"] = price_source

        rating = agg.get("ratingValue")
        rating_count = agg.get("ratingCount")
        review_count = agg.get("reviewCount")
        if not rating:
            m = re.search(r"(\d\.\d)\s*★?\s*\n?\s*\|?\s*([\d,]+)", text)
            if m:
                rating, rating_count = m.group(1), m.group(2)
        m = re.search(r"([\d,]+)\s+Ratings?\s*(?:&|and)\s*([\d,]+)\s+Reviews?", text, re.I)
        if m:
            rating_count, review_count = m.group(1), m.group(2)
        data["ratings"] = {
            "average_rating": to_number(rating),
            "total_ratings": to_number(rating_count),
            "total_reviews": to_number(review_count),
            "breakdown": parse_breakdown(text),
        }

        end_all = [r"^EMI offers", r"^Instant loan offers", r"^Apply for", r"^Bank offers",
                   r"^Exchange offer", r"^Additional benefits", r"^Delivery details"]
        bank_blk = section(text, r"^Bank offers\s*$", [e for e in end_all if "Bank" not in e])
        emi_blk = section(text, r"^EMI offers\s*$", [e for e in end_all if "EMI" not in e])
        loan_blk = section(text, r"^Instant loan offers\s*$", [e for e in end_all if "loan" not in e])
        exch_blk = section(text, r"^Exchange offer\s*$", end_all[:3] + [r"^Bank offers"])
        old_style = re.findall(r"^(?:Bank Offer|Partner Offer|Special Price)\s*(.+)$", text, re.I | re.M)
        data["offers"] = {
            "bank_offers": group_offers(bank_blk, r"^₹[\d,]+\s*off|^\d+%") or old_style,
            "emi_offers": group_offers(emi_blk, r"^₹[\d,]+/m"),
            "instant_loan_offers": group_offers(loan_blk, r"^₹[\d,]+/m"),
            "exchange_offer": " | ".join(l for l in exch_blk.splitlines() if l.strip() and l.strip() != "Apply"),
            "additional_benefits": group_offers(
                section(text, r"^Additional benefits on this purchase\s*$", [r"^Delivery details"]), r"^$"),
        }
        data["offers"]["no_cost_emi_mentioned"] = bool(re.search(r"no[\s-]*cost\s*emi", text, re.I))
        data["offers"]["coupon_mentioned"] = bool(re.search(r"\bcoupon\b", text, re.I))

        deliv = re.search(r"Delivery by\s*([^\n]+)", text)
        seller = re.search(r"Seller:\s*([^\n]+)\n?\s*([\d.]+)\s*★?", text)
        warranty_lines = list(dict.fromkeys(re.findall(r"^.*\bwarranty\b.*$", text, re.I | re.M)))[:8]
        data["delivery"] = {
            "estimate": deliv.group(1).strip() if deliv else None,
            "address_line": safe(lambda: re.search(r"Delivery details\s*\n(.+)", text).group(1).strip()),
            "free_delivery": bool(re.search(r"Free\s*Delivery", text, re.I)),
        }
        data["seller"] = {
            "name": seller.group(1).strip() if seller else None,
            "rating": to_number(seller.group(2)) if seller else None,
        }
        data["warranty_and_policies"] = {
            "warranty": [w.strip() for w in warranty_lines],
            "return_or_replacement": re.findall(r"\d+\s*Days?\s*(?:Replacement|Return)[^\n]*", text, re.I)[:2],
            "cash_on_delivery": bool(re.search(r"Cash on\s*Delivery", text, re.I)),
            "flipkart_assured": bool(re.search(r"Flipkart\s*Assured", text, re.I)),
        }

        features = safe(lambda: page.evaluate(JS_HIGHLIGHTS), []) or []
        if not features and prod.get("description"):
            features = [l.strip() for l in prod["description"].splitlines() if l.strip()]
        data["features"] = features
        data.update(build_specs(page, text))
        data["manufacturer"] = extract_manufacturer(text, data.get("specifications_flat", {}))
        if debug or not data["price"].get("mrp") or not (
                data["manufacturer"].get("details") or data["manufacturer"].get("raw_text")):
            with open("debug_product_page.txt", "w", encoding="utf-8") as f:
                f.write(text)
            safe(lambda: page.screenshot(path="debug_product_page.png", full_page=True))
            print("  [debug] MRP/discount or manufacturer info not found -> "
                  "saved debug_product_page.txt / .png")

        imgs, vids = collect_media(page)
        if prod.get("image"):
            extra = prod["image"] if isinstance(prod["image"], list) else [prod["image"]]
            imgs = dedupe_images(imgs + extra)
        data["images"] = imgs
        data["videos"] = vids
        print(f"  Media: {len(imgs)} product images, {len(vids)} product videos")
        data["variants"] = safe(lambda: page.evaluate(JS_VARIANTS), {}) or {}

        data["review_summary"] = {}
        data["reviews"] = []
        if with_reviews:
            print("Scraping reviews ...")
            try:
                res = scrape_review_section(
                    page, data["url"], max_review_pages, sort, debug)
            except Exception:
                print("  [ERROR] review scraping crashed:", file=sys.stderr)
                traceback.print_exc()
                res = None
            if res:
                data["review_summary"], data["reviews"] = res
                data["review_summary"]["reviews_scraped"] = len(data["reviews"])

        browser.close()
        return data


# --------------------------------------------------------------------------- #
# NORMALIZER  -  raw scrape  ->  ONE product record, reviews/comments as an array
# --------------------------------------------------------------------------- #


def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def slug_upper(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (s or "").upper()).strip("_")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(amount, currency):
    return {"amount": amount, "currency": currency} if amount is not None else None


def find_spec(flat: dict, *names):
    """Look up a value in specifications_flat ('Section | Key': value) by key name."""
    for want in names:
        for k, v in flat.items():
            key = k.split(" | ", 1)[-1].strip().lower()
            if key == want.lower() and v:
                return v
    return None


def parse_capacity(s):
    if not s:
        return {"value": None, "unit": None}
    m = re.search(r"([\d.]+)\s*([A-Za-z]+)", str(s))
    if not m:
        return {"value": None, "unit": None}
    unit = m.group(2).lower()
    unit = {"l": "litre", "ltr": "litre", "litres": "litre", "liter": "litre", "liters": "litre",
            "litre": "litre"}.get(unit, unit)
    return {"value": to_number(m.group(1)), "unit": unit}


def parse_energy_rating(s):
    if not s:
        return None
    m = re.search(r"([1-5])\s*[-]?\s*star", str(s), re.I)
    if m:
        return f"{m.group(1)}_star"
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or None


def parse_warranty(data: dict):
    flat = data.get("specifications_flat", {})
    candidates = [find_spec(flat, "Warranty Summary", "Warranty", "Warranty Period", "Domestic Warranty")]
    candidates += data.get("warranty_and_policies", {}).get("warranty", [])
    for text in filter(None, candidates):
        m = re.search(r"(\d+)\s*(year|yr|month)", text, re.I)
        if m:
            n = int(m.group(1))
            months = n * 12 if m.group(2).lower().startswith("y") else n
            wtype = "manufacturer" if re.search(r"manufacturer|brand", text, re.I) else None
            return months, wtype
    return None, None


def availability_status(raw):
    r = (raw or "").lower()
    if "instock" in r:
        return "in_stock"
    if "outofstock" in r or "soldout" in r:
        return "out_of_stock"
    if "preorder" in r:
        return "pre_order"
    return None


OPTIONAL_PATHS = [
    "content.title", "entities.products.0.sku", "entities.brands",
    "product_attributes.model_number", "product_attributes.color",
    "product_attributes.energy_rating", "product_attributes.manufacturer.details",
    "commercial.price.selling_price", "commercial.price.original_price",
    "commercial.price.discount_percentage", "commercial.warranty.duration_months",
    "metrics.rating.value", "metrics.review_count", "reviews",
]


def _missing(rec: dict, path: str) -> bool:
    cur = rec
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return True
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return True
    return cur in (None, "", [], {})


def build_review_item(r: dict, product_id, ctx: dict) -> dict:
    """One element of the record's `reviews` array."""
    key = "|".join(str(x) for x in (product_id, r["reviewer_name"], r["reviewer_location"],
                                    r["rating"], r["title"], r["review_text"]))
    published = f"{r['posted_date']}T00:00:00Z" if r["posted_date"] else None
    city = r["reviewer_location"] or None
    return {
        "review_id": "review_" + sha256(key)[:16],
        "title": r["title"] or None,
        "text": r["review_text"] or None,
        "language": ctx["language"],
        "rating": {"value": r["rating"], "scale_min": 1, "scale_max": 5},
        "published_at": published,
        "posted_relative": r["posted_relative"],
        "author": {
            "source_author_id_hash": (
                "sha256:" + sha256(f"flipkart|{r['reviewer_name']}|{r['reviewer_location']}")
                if r["reviewer_name"] else None),
            "display_name": r["reviewer_name"] or None,
            "author_type": "customer",
            "is_verified_purchase": r["verified_purchase"],
        },
        "location": {
            "country_code": "IN",
            "city": city,
            "location_precision": "city" if city else "country",
        },
        "variant_reviewed": r["variant_reviewed"],
        "variant_reviewed_raw": r["variant_reviewed_raw"],
        "helpful_votes": r["helpful_count"],
        "second_reaction_count": r["second_reaction_count"],
        "content_hash": "sha256:" + sha256(f"{r['title']}|{r['review_text']}|{r['reviewer_name']}"),
    }


def build_product_record(data: dict, reviews: list, ctx: dict) -> dict:
    now_iso = iso_utc(ctx["now"])
    price = data.get("price", {})
    offers = data.get("offers", {})
    flat = data.get("specifications_flat", {})
    pol = data.get("warranty_and_policies", {})
    deliv = data.get("delivery", {})
    seller = data.get("seller", {})
    ratings = data.get("ratings", {})
    rsum = data.get("review_summary", {}) or {}
    currency = price.get("currency") or "INR"
    product_id = data.get("product_id") or data.get("item_id") or "product"

    rec_id = f"{ctx['tenant_id']}:flipkart:{product_id}"

    # ---- brand --------------------------------------------------------------
    brand_name = data.get("brand")
    brands = []
    if brand_name:
        if ctx["client_brand"]:
            role = "client" if brand_name.strip().lower() == ctx["client_brand"].strip().lower() else "competitor"
        else:
            role = "unspecified"
        brands = [{"brand_id": "BRAND_" + slug_upper(brand_name), "brand_name": brand_name, "brand_role": role}]

    model_number = find_spec(flat, "Model Number", "Model Name", "Model")
    color = find_spec(flat, "Color", "Colour")
    cap = parse_capacity(find_spec(flat, "Capacity", "Total Capacity", "Refrigerator Capacity"))
    energy = parse_energy_rating(find_spec(flat, "Energy Rating", "Star Rating", "BEE Rating"))

    # ---- price / discount ---------------------------------------------------
    listed = price.get("selling_price")
    original = price.get("mrp")
    final_selling = price.get("special_deal_price") or listed
    disc_amt = disc_pct = None
    if original and final_selling and original > final_selling:
        disc_amt = round(original - final_selling, 2)
        disc_pct = round(disc_amt / original * 100, 2)
    elif price.get("discount_percent"):
        disc_pct = to_number(price.get("discount_percent"))
    is_promo = bool(disc_amt or disc_pct or offers.get("bank_offers"))

    warranty_months, warranty_type = parse_warranty(data)
    free_delivery = deliv.get("free_delivery")
    mfr = data.get("manufacturer") or {}

    rec = {
        "_id": rec_id,
        "schema_version": SCHEMA_VERSION,
        "record_type": "social_mention",

        "tenant": {
            "tenant_id": ctx["tenant_id"],
            "project_id": ctx["project_id"],
            "report_scope_ids": ctx["report_scope_ids"],
        },

        "source": {
            "platform": "flipkart",
            "channel": "ecommerce_review",
            "source_type": "product_listing",
            "source_item_id": product_id,
            "source_url": data.get("url"),
            "parent_item_id": None,
            "published_at": None,
            "collected_at": now_iso,
            "updated_at": None,
            "country_code": "IN",
            "marketplace": "flipkart.com",
        },

        "content": {
            "title": data.get("product_name"),
            "text": data.get("description"),
            "language": ctx["language"],
            "original_language": ctx["language"],
            "content_type": "product_listing",
            "media": (
                [{"type": "image", "url": u} for u in data.get("images", [])]
                + [{"type": "video", "url": u} for u in data.get("videos", [])]),
            "hashtags": [],
            "mentions": [],
            "keywords": data.get("features", []),
        },

        "entities": {
            "brands": brands,
            "products": [{
                "product_id": product_id,
                "product_name": data.get("product_name"),
                "sku": data.get("sku"),
                "model_number": model_number,
                "product_url": data.get("url"),
            }],
            "retailer": {"retailer_id": "RETAILER_FLIPKART", "retailer_name": "Flipkart"},
            "other_brands": [],
        },

        "taxonomy": {
            "industry": ctx["industry"],
            "category": ctx["category"],
            "subcategory": ctx["subcategory"],
            "product_type": ctx["product_type"],
            "taxonomy_version": ctx["taxonomy_version"],
            "mapping_method": "cli_arguments" if ctx["category"] else None,
            "mapping_status": "mapped" if ctx["category"] else "unmapped",
        },

        "product_attributes": {
            "model_number": model_number,
            "capacity": cap,
            "color": color,
            "energy_rating": energy,
            "manufacturer": {
                "name": mfr.get("name"),
                "details": mfr.get("details"),
                "marketed_by": mfr.get("marketed_by"),
                "packed_by": mfr.get("packed_by"),
                "imported_by": mfr.get("imported_by"),
                "country_of_origin": mfr.get("country_of_origin"),
                "generic_name": mfr.get("generic_name"),
                "net_quantity": mfr.get("net_quantity"),
                "manufacturing_date": mfr.get("manufacturing_date"),
                "customer_care": mfr.get("customer_care"),
                "raw_text": mfr.get("raw_text"),
            },
            "variants": data.get("variants", {}),
            "specifications": data.get("specifications", {}),
        },

        "commercial": {
            "price": {
                "listed_price": money(listed, currency),
                "selling_price": money(final_selling, currency),
                "original_price": money(original, currency),
                "discount_amount": disc_amt,
                "discount_percentage": disc_pct,
                "extra_discount_amount": price.get("extra_discount_amount"),
                "price_type": "promotional" if is_promo else "regular",
                "price_observed_at": iso_utc(datetime.fromisoformat(data["scraped_at"]))
                if data.get("scraped_at") else now_iso,
            },
            "price_range": {
                "minimum": None, "maximum": None, "currency": currency,
                "period_start": None, "period_end": None,
            },
            "promotion": {
                "is_promotional": is_promo,
                "promotion_type": "discount" if (disc_amt or disc_pct) else (
                    "bank_offer" if offers.get("bank_offers") else None),
                "promotion_name": None,
                "coupon_available": bool(offers.get("coupon_mentioned")),
                "coupon_code": None,
            },
            "offers_detail": {
                "bank_offers": offers.get("bank_offers", []),
                "emi_offers": offers.get("emi_offers", []),
                "instant_loan_offers": offers.get("instant_loan_offers", []),
                "exchange_offer": offers.get("exchange_offer") or None,
                "additional_benefits": offers.get("additional_benefits", []),
            },
            "payment_options": {
                "emi_available": bool(offers.get("emi_offers")),
                "no_cost_emi": bool(offers.get("no_cost_emi_mentioned")),
                "bank_offer_available": bool(offers.get("bank_offers")),
                "exchange_offer_available": bool(offers.get("exchange_offer")),
                "cash_on_delivery": bool(pol.get("cash_on_delivery")),
            },
            "availability": {
                "status": availability_status(price.get("availability")),
                "stock_quantity": None,
            },
            "delivery": {
                "delivery_charge": money(0, currency) if free_delivery else None,
                "estimated_delivery_date": None,
                "delivery_type": "free_delivery" if free_delivery else None,
            },
            "warranty": {
                "duration_months": warranty_months,
                "warranty_type": warranty_type,
            },
        },

        "metrics": {
            "rating": {"value": ratings.get("average_rating"), "scale_min": 1, "scale_max": 5},
            "engagement": {
                "likes": None, "comments": None, "shares": None, "views": None, "reactions": None,
            },
            "helpful_votes": None,
            "review_count": ratings.get("total_reviews") or rsum.get("total_reviews") or len(reviews),
            "author_followers": None,
            "reach": None,
        },

        # product-level author block = the seller
        "author": {
            "source_author_id_hash": None,
            "display_name": seller.get("name"),
            "author_type": "seller",
            "is_verified_purchase": None,
            "profile_url": None,
            "follower_count": None,
        },

        "location": {
            "country_code": "IN", "region": None, "city": None, "location_precision": "country",
        },

        "campaign": {
            "campaign_name": None, "campaign_type": None, "event_name": None, "campaign_id": None,
        },

        # ---- all comments / reviews + review statistics, as arrays/objects ----
        "review_summary": {
            "total_ratings": ratings.get("total_ratings") or rsum.get("total_ratings"),
            "total_reviews": ratings.get("total_reviews") or rsum.get("total_reviews"),
            "average_rating": ratings.get("average_rating") or rsum.get("average_rating"),
            "rating_breakdown": ratings.get("breakdown") or rsum.get("rating_breakdown") or {},
            "positive_feedback_percent": rsum.get("positive_feedback_percent"),
            "negative_feedback_percent": rsum.get("negative_feedback_percent"),
            "aspect_ratings": rsum.get("aspect_ratings", {}),
            "reviews_collected": len(reviews),
            "sort_used": rsum.get("sort_used"),
            "pages_scraped": rsum.get("pages_scraped"),
            "stop_reason": rsum.get("stop_reason"),
            "review_url": rsum.get("review_url"),
        },
        "reviews": reviews,

        "deduplication": {
            "content_hash": "sha256:" + sha256(f"{product_id}|{data.get('product_name')}"),
            "duplicate_status": "unique",
            "canonical_record_id": rec_id,
            "duplicate_of": None,
        },

        "provenance": {
            "collection_run_id": ctx["run_id"],
            "connector_name": CONNECTOR_NAME,
            "connector_version": CONNECTOR_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "source_payload_ref": None,
            "ingestion_timestamp": now_iso,
        },

        "quality": {
            "status": "accepted",
            "is_valid": True,
            "is_relevant": True,
            "validation_errors": [],
            "missing_optional_fields": [],
        },

        "lifecycle": {
            "first_seen_at": now_iso,
            "last_seen_at": now_iso,
            "record_status": "active",
        },
    }

    errors = []
    if not (data.get("product_id") or data.get("item_id")):
        errors.append("missing_product_id")
    if not data.get("product_name"):
        errors.append("missing_product_name")
    rec["quality"]["validation_errors"] = errors
    rec["quality"]["is_valid"] = not errors
    rec["quality"]["status"] = "accepted" if not errors else "rejected"
    rec["quality"]["missing_optional_fields"] = [p for p in OPTIONAL_PATHS if _missing(rec, p)]
    return rec


def normalize(data: dict, args) -> dict:
    now = datetime.now(timezone.utc)
    ctx = {
        "now": now,
        "run_id": "RUN_" + now.strftime("%Y%m%d_%H%M%S"),
        "tenant_id": args.tenant_id,
        "project_id": args.project_id,
        "report_scope_ids": [s.strip() for s in args.report_scope_ids.split(",") if s.strip()],
        "client_brand": args.client_brand,
        "industry": args.industry,
        "category": args.category,
        "subcategory": args.subcategory,
        "product_type": args.product_type,
        "taxonomy_version": args.taxonomy_version,
        "language": args.language,
    }
    product_id = data.get("product_id") or data.get("item_id") or "product"
    reviews, seen = [], set()
    for r in data.get("reviews", []):
        item = build_review_item(r, product_id, ctx)
        if item["review_id"] in seen:  # identical review already collected
            continue
        seen.add(item["review_id"])
        reviews.append(item)
    return build_product_record(data, reviews, ctx)


def write_output(record: dict, fname: str, fmt: str):
    with open(fname, "w", encoding="utf-8") as f:
        if fmt == "jsonl":
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            json.dump(record, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="Flipkart product link")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--no-reviews", action="store_true", help="skip the reviews page")
    ap.add_argument("--sort", choices=list(SORT_PARAM), default="recent",
                    help="review order (default: recent)")
    ap.add_argument("--max-review-pages", type=int, default=300,
                    help="safety cap on review pages (10 reviews per page)")
    ap.add_argument("--debug", action="store_true", help="save page text when scraping stops early")

    g = ap.add_argument_group("schema / tenant options")
    g.add_argument("--tenant-id", default="tenant_001")
    g.add_argument("--project-id", default="retail_listening_001")
    g.add_argument("--report-scope-ids", default="", help="comma separated, e.g. TND_Q2_FY27")
    g.add_argument("--client-brand", default=None,
                   help="brand name of the client; scraped brand = this -> 'client', else 'competitor'")
    g.add_argument("--industry", default=None)
    g.add_argument("--category", default=None)
    g.add_argument("--subcategory", default=None)
    g.add_argument("--product-type", default=None)
    g.add_argument("--taxonomy-version", default="TND_2026_V1")
    g.add_argument("--language", default="en", help="language code written to content.language")
    g.add_argument("--format", choices=["json", "jsonl"], default="json",
                   help="json = one object (default), jsonl = one line")
    g.add_argument("--output", default=None, help="output file name (default flipkart_<product_id>.json)")
    g.add_argument("--save-raw", action="store_true", help="also save the raw scrape as flipkart_raw_<id>.json")
    args = ap.parse_args()

    url = args.url or input("Enter Flipkart product link: ").strip()
    if "flipkart.com" not in url:
        sys.exit("That does not look like a Flipkart link.")

    print("Scraping product page ...")
    data = scrape(url, headed=args.headed, with_reviews=not args.no_reviews,
                  max_review_pages=args.max_review_pages, sort=args.sort,
                  debug=args.debug)

    pid = data.get("product_id") or data.get("item_id") or "product"

    if args.save_raw:
        raw_name = f"flipkart_raw_{pid}.json"
        with open(raw_name, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Saved raw -> {raw_name}")

    record = normalize(data, args)
    if not record["reviews"]:
        print("[warn] 0 reviews were scraped - 'reviews' is an empty array "
              "(see debug_reviews_page_1.txt if it was saved).")

    ext = "jsonl" if args.format == "jsonl" else "json"
    fname = args.output or f"flipkart_{pid}.{ext}"
    write_output(record, fname, args.format)
    print(f"Saved 1 product record with {len(record['reviews'])} reviews -> {fname}")


if __name__ == "__main__":
    main()