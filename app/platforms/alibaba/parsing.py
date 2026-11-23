# from __future__ import annotations

# import hashlib
# import re
# import urllib.parse
# from datetime import datetime
# from decimal import Decimal, InvalidOperation
# from typing import Any

# _BAD_NAMES = {"", "n/a", "na", "null", "none", "-", "unknown"}

# def product_id_from_href(href: str) -> str:
#     m = re.search(r"_(\d{8,})(?:\.html|$|\?)", href)
#     if m:
#         return m.group(1)
#     qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
#     pid = (qs.get("productId") or qs.get("offerId") or [""])[0]
#     if pid:
#         return pid
#     clean = href.split("?")[0].rstrip("/")
#     return hashlib.md5(clean.encode()).hexdigest()[:16]

# def _norm_name(name: Any) -> str:
#     return re.sub(r"\s+", " ", str(name or "").strip().lower())

# def resolve_supplier_identity(company_id, card_name, detail_name) -> dict:
#     real_name = detail_name if _norm_name(detail_name) not in _BAD_NAMES else card_name
#     norm = _norm_name(real_name)

#     cid = None
#     if company_id not in (None, "", 0, "0"):
#         try:
#             cid = int(company_id)
#         except (TypeError, ValueError):
#             cid = str(company_id)

#     if cid is not None:
#         supplier_id = f"ali-{cid}"
#     elif norm not in _BAD_NAMES:
#         supplier_id = "name-" + hashlib.md5(norm.encode()).hexdigest()[:12]
#     else:
#         supplier_id = None

#     return {
#         "supplier_id": supplier_id,
#         "company_id": cid,
#         "supplier_name": real_name if norm not in _BAD_NAMES else None,
#         "raw": {"card_name": card_name, "detail_name": detail_name},
#     }

# _DATE_FORMATS = (
#     "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
#     "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
# )

# def parse_posted_date(s: str | None):
#     if not s or s == "N/A":
#         return None
#     s = re.sub(r"(?i)^\s*(posted|updated|published)[:\s]+", "", s.strip())
#     for fmt in _DATE_FORMATS:
#         try:
#             return datetime.strptime(s, fmt).date()
#         except ValueError:
#             continue
#     m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
#     if m:
#         try:
#             return datetime(int(m[1]), int(m[2]), int(m[3])).date()
#         except ValueError:
#             return None
#     m = re.search(r"([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
#     if m:
#         try:
#             return datetime.strptime(f"{m[1][:3]} {m[2]} {m[3]}", "%b %d %Y").date()
#         except ValueError:
#             return None
#     return None

# _CURRENCY_SYMBOLS = {
#     "US$": "USD", "C$": "CAD", "A$": "AUD", "₹": "INR", "$": "USD",
#     "€": "EUR", "£": "GBP", "¥": "CNY", "₫": "VND", "₩": "KRW",
# }
# _CURRENCY_WORDS = {
#     "rs": "INR", "inr": "INR", "usd": "USD", "cny": "CNY",
#     "rmb": "CNY", "eur": "EUR", "gbp": "GBP",
# }

# def _to_decimal(num: str):
#     try:
#         return Decimal(num.replace(",", "").strip())
#     except (InvalidOperation, ValueError, AttributeError):
#         return None

# def dec_num(d):
#     if not isinstance(d, Decimal):
#         return None
#     return int(d) if d == d.to_integral_value() else float(d)

# def parse_price(raw) -> dict:
#     out = {
#         "price_raw": raw if raw not in (None, "", "N/A") else None,
#         "price_min": None, "price_max": None, "currency": None, "price_unit": None,
#         "price_extraction_status": "missing",
#     }
#     if not raw or str(raw).strip() in ("", "N/A"):
#         return out
#     text = str(raw).strip()

#     for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
#         if sym in text:
#             out["currency"] = _CURRENCY_SYMBOLS[sym]
#             break
#     if out["currency"] is None:
#         wm = re.search(r"\b(rs|inr|usd|cny|rmb|eur|gbp)\b", text, re.I)
#         if wm:
#             out["currency"] = _CURRENCY_WORDS[wm.group(1).lower()]

#     um = re.search(r"/\s*([A-Za-z][A-Za-z ]{0,15})", text)
#     if um:
#         out["price_unit"] = um.group(1).strip().lower()

#     nums = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
#     decs = [d for d in (_to_decimal(n) for n in nums) if d is not None]
#     if not decs:
#         out["price_extraction_status"] = "unparseable"
#         return out
#     if len(decs) >= 2 and re.search(r"[-–—]|\bto\b", text):
#         out["price_min"] = dec_num(min(decs))
#         out["price_max"] = dec_num(max(decs))
#         out["price_extraction_status"] = "range_parsed"
#     else:
#         out["price_min"] = out["price_max"] = dec_num(decs[0])
#         out["price_extraction_status"] = "parsed"
#     return out

# _UNIT_SINGULAR = {
#     "pieces": "piece", "pcs": "piece", "pc": "piece", "sets": "set", "units": "unit",
#     "kilograms": "kilogram", "kgs": "kilogram", "kg": "kilogram", "tons": "ton",
#     "tonnes": "tonne", "meters": "meter", "metres": "meter", "pairs": "pair",
#     "boxes": "box", "bags": "bag", "rolls": "roll", "cartons": "carton",
#     "dozens": "dozen", "acres": "acre", "liters": "liter", "litres": "liter",
# }

# def parse_moq(raw) -> dict:
#     out = {
#         "moq_raw": raw if raw not in (None, "", "N/A") else None,
#         "moq_value": None, "moq_unit": None, "moq_extraction_status": "missing",
#     }
#     if not raw or str(raw).strip() in ("", "N/A"):
#         return out
#     m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]+)", str(raw))
#     if not m:
#         out["moq_extraction_status"] = "unparseable"
#         return out
#     val = _to_decimal(m.group(1))
#     if val is None:
#         out["moq_extraction_status"] = "unparseable"
#         return out
#     out["moq_value"] = dec_num(val)
#     unit = m.group(2).lower()
#     out["moq_unit"] = _UNIT_SINGULAR.get(unit, unit)
#     out["moq_extraction_status"] = "parsed"
#     return out

# _ICON_RE = re.compile(
#     r"\.svg(\?|$)|/flags?/|/common/|/mobile/|tps-\d{1,3}-\d{1,3}\.|"
#     r"-55-tps-|-56-tps-|icon|sprite|placeholder|avatar",
#     re.I,
# )

# def _is_icon(u: str) -> bool:
#     return not u or not u.startswith("http") or bool(_ICON_RE.search(u))

# def classify_images(card_images, product_media, supplier_logo) -> dict:
#     pm_imgs = (product_media or {}).get("images") or []
#     product_imgs: list[str] = []
#     seen: set[str] = set()
#     for u in pm_imgs:
#         if u and u not in seen and not _is_icon(u):
#             seen.add(u)
#             product_imgs.append(u)
#     if not product_imgs:
#         for u in card_images or []:
#             if u and u not in seen and not _is_icon(u):
#                 seen.add(u)
#                 product_imgs.append(u)
#     other: list[str] = []
#     for u in card_images or []:
#         if u and u not in seen and not _is_icon(u) and u != supplier_logo:
#             seen.add(u)
#             other.append(u)
#     if product_imgs:
#         status = "classified"
#     elif card_images:
#         status = "icons_only"
#     else:
#         status = "missing"
#     return {
#         "main_image_url": product_imgs[0] if product_imgs else None,
#         "product_image_urls": product_imgs,
#         "supplier_logo_url": supplier_logo or None,
#         "other_media_urls": other,
#         "image_extraction_status": status,
#     }

# _MODEL_JUNK = {
#     "number", "no", "no.", "num", "num.", "model", "model number",
#     "n/a", "na", "-", "null", "none", "",
# }
# _MODEL_LABELS = ("model number", "model no.", "model no", "model")
# _CPU_LIKE_RE = re.compile(
#     r"^(i[3579][\s-]?\d{2,5}[a-z]*|celeron|pentium|atom|ryzen|athlon|"
#     r"core\s*(i|ultra|m)|xeon|snapdragon|mediatek|mt\d|"
#     r"n\d{3,4}[a-z]*|j\d{3,4}[a-z]*|a\d{3,4}[a-z]*)$",
#     re.I,
# )
# _GENERIC_BRANDS = {
#     "oem", "odm", "oem/odm", "none", "n/a", "na", "null", "-", "generic",
#     "no brand", "nobrand", "unbranded", "other", "others", "customized", "custom", "",
# }

# def sanitize_model_number(v):
#     if v in (None, "", "N/A"):
#         return None
#     s = str(v).strip()
#     if s.lower() in _MODEL_JUNK:
#         return None
#     return s

# def _norm_ident(s) -> str:
#     return re.sub(r"\s+", " ", str(s or "").strip()).lower()

# def _cpu_spec_values(specs: dict) -> list[str]:
#     out = []
#     for k, v in (specs or {}).items():
#         kl = str(k).lower()
#         if ("processor" in kl or "cpu" in kl) and v not in (None, "", "N/A"):
#             out.append(str(v))
#     return out

# def _model_candidates(ex: dict) -> list[tuple[str, str]]:
#     cands: list[tuple[str, str]] = []
#     for v in ex.get("variants") or []:
#         if _norm_ident(v.get("name")) in _MODEL_LABELS:
#             vals = [x for x in (v.get("values") or []) if x]
#             if vals:
#                 cands.append(("variant", str(vals[0]).strip()))
#                 break
#     for k, val in (ex.get("specifications") or {}).items():
#         if _norm_ident(k) in _MODEL_LABELS and val not in (None, "", "N/A"):
#             cands.append(("specification", str(val).strip()))
#             break
#     return cands

# def _is_cpu_value(model: str, cpu_values: list[str]) -> str | None:
#     nm = _norm_ident(model)
#     if not nm:
#         return None
#     for cv in cpu_values:
#         if _norm_ident(cv) == nm:
#             return cv
#     if _CPU_LIKE_RE.match(model.strip()) and " " not in nm:
#         for cv in cpu_values:
#             toks = set(re.findall(r"[a-z0-9][a-z0-9\-]*", cv.lower()))
#             if nm in toks and _norm_ident(cv) != nm:
#                 return cv
#     return None

# def validate_model_number(ex: dict) -> dict:
#     specs = ex.get("specifications") or {}
#     cpu_values = _cpu_spec_values(specs)
#     raw_cands = _model_candidates(ex)

#     cleaned: list[tuple[str, str]] = []
#     for src, val in raw_cands:
#         s = sanitize_model_number(val)
#         if s is not None:
#             cleaned.append((src, s))

#     if not cleaned:
#         return {"value": None, "status": "not_provided", "source": None, "raw": None,
#                 "candidates": [], "reason": "no model-number attribute on page",
#                 "collides_with": None, "confidence": None}

#     all_values = [c for _, c in cleaned]
#     collisions = [(src, val, _is_cpu_value(val, cpu_values)) for src, val in cleaned]
#     non_cpu = [(src, val) for (src, val, col) in collisions if col is None]

#     if not non_cpu:
#         first_src, first_val, first_col = collisions[0]
#         return {"value": None, "status": "invalid", "source": first_src, "raw": first_val,
#                 "candidates": all_values,
#                 "reason": f"candidate {first_val!r} equals processor/CPU value",
#                 "collides_with": first_col, "confidence": None}

#     top_src, top_val, top_col = collisions[0]
#     if top_col is not None:
#         return {"value": None, "status": "not_verified", "source": top_src, "raw": top_val,
#                 "candidates": all_values,
#                 "reason": f"authoritative model source was a CPU value ({top_val!r})",
#                 "collides_with": top_col, "confidence": None}

#     distinct = {_norm_ident(v) for _, v in non_cpu}
#     status = "conflicting" if len(distinct) > 1 else "available"
#     if status == "conflicting":
#         confidence = "low"
#     else:
#         confidence = "high" if top_src == "variant" else "medium"
#     reason = (f"multiple distinct model candidates {[v for _, v in non_cpu]}"
#               if status == "conflicting" else None)
#     return {"value": top_val, "status": status, "source": top_src, "raw": top_val,
#             "candidates": all_values, "reason": reason, "collides_with": None,
#             "confidence": confidence}

# def sanitize_brand(v) -> dict:
#     if v in (None, "", "N/A"):
#         return {"value": None, "status": "missing_on_source", "raw": None,
#                 "reason": "no brand attribute on page"}
#     s = str(v).strip()
#     if s.lower() in _GENERIC_BRANDS:
#         return {"value": None, "status": "not_verified", "raw": s,
#                 "reason": f"generic value {s!r}; consumer brand not established"}
#     return {"value": s, "status": "available", "raw": s, "reason": None}

# _UI_NOISE_RE = re.compile(
#     r"^(what are you looking for|all categories|know your supplier|search|sort by|"
#     r"based on\b|most relevant|related searches|frequently bought|you may|about this|"
#     r"product details|see product details|helpful|verified purchase|repeat buyer|"
#     r"get the app|sign in|join free|trade assurance|categories|home|"
#     r"previous slide|next slide|prev slide|previous|next|slide \d+|"
#     r"min\.?\s*order|minimum order|moq\b|min\.? order quantity|"
#     r"payment|shipping|logistics|add to cart|contact supplier|chat now|"
#     r"view (more|all)|show (more|original)|report|share|save)\b",
#     re.I,
# )
# _TAGBAR_RE = re.compile(r"^(?:[\w ]+\(\d+\))(?:\s*[\w ]+\(\d+\)){1,}$")
# _REPLY_RE = re.compile(r"(?i)supplier'?s?\s*repl(?:y|ies)\s*:?\s*")

# def split_supplier_reply(text):
#     if not isinstance(text, str):
#         return text, None
#     m = _REPLY_RE.search(text)
#     if not m:
#         return text, None
#     customer = text[: m.start()].strip() or "N/A"
#     reply = text[m.end():].strip() or None
#     return customer, reply

# def _looks_like_ui(text) -> bool:
#     t = str(text or "").strip()
#     if not t or t == "N/A":
#         return False
#     return bool(_UI_NOISE_RE.match(t)) or bool(_TAGBAR_RE.match(t))

# def parse_stars(raw):
#     if raw in (None, "", "N/A"):
#         return None
#     if isinstance(raw, (int, float)):
#         v = round(float(raw))
#         return v if 1 <= v <= 5 else None
#     m = re.search(r"([0-5](?:\.\d)?)", str(raw))
#     if not m:
#         return None
#     v = round(float(m.group(1)))
#     return v if 1 <= v <= 5 else None

# def dedupe(seq) -> list:
#     seen, out = set(), []
#     for x in seq or []:
#         if x and x not in seen:
#             seen.add(x)
#             out.append(x)
#     return out

# def validate_review(item: dict) -> dict:
#     warnings: list[str] = []
#     reviewer = item.get("reviewer", "N/A")
#     country = item.get("country", "N/A")
#     rating = parse_stars(
#         item.get("rating") if item.get("rating") is not None else item.get("stars")
#     )

#     ctext, _ = split_supplier_reply(item.get("text", "N/A"))
#     media = item.get("media") or item.get("review_media") or {}
#     media_present = bool(media.get("images") or media.get("videos"))

#     clean_reviewer = None
#     if _looks_like_ui(reviewer):
#         warnings.append(f"reviewer looks like UI text: {reviewer!r}")
#     elif reviewer not in (None, "", "N/A"):
#         clean_reviewer = reviewer

#     clean_country = None
#     if _looks_like_ui(country):
#         warnings.append(f"country looks like UI text: {country!r}")
#     elif country not in (None, "", "N/A"):
#         clean_country = country

#     text_is_ui = _looks_like_ui(ctext)
#     if text_is_ui:
#         warnings.append(f"review text looks like UI/page content: {str(ctext)[:40]!r}")

#     genuine_text = ctext not in (None, "", "N/A") and not text_is_ui
#     has_content = (
#         genuine_text
#         or (rating is not None)
#         or (media_present and clean_reviewer is not None)
#     )
#     return {"valid": bool(has_content), "rating": rating, "warnings": warnings,
#             "clean_reviewer": clean_reviewer, "clean_country": clean_country}

# def review_quality(block: dict | None, tab_count: int, complete: bool = False) -> dict:
#     if block is None:
#         status = "no_reviews_reported" if tab_count == 0 else "unavailable"
#         return {"items": [], "status": status, "total_displayed": tab_count,
#                 "extracted": 0, "valid": 0, "rejected": 0, "missing_ratings": 0,
#                 "warnings": []}

#     raw = block.get("reviews", []) or []
#     clean, warnings = [], []
#     rejected_n = 0
#     for it in raw:
#         res = validate_review(it)
#         warnings.extend(res["warnings"])
#         if not res["valid"]:
#             rejected_n += 1
#             continue
#         it = dict(it)
#         it["reviewer"] = res["clean_reviewer"] or "N/A"
#         it["country"] = res["clean_country"] or "N/A"
#         it["rating"] = res["rating"]
#         it["valid"] = True
#         clean.append(it)

#     extracted = valid_n = len(clean)
#     missing_ratings = sum(1 for it in clean if it.get("rating") is None)

#     if complete:
#         displayed = valid_n
#         status = "available" if valid_n > 0 else "no_reviews_reported"
#     else:
#         displayed = max(tab_count, valid_n) if tab_count > 0 else valid_n
#         if displayed == 0:
#             status = "no_reviews_reported"
#         elif valid_n < displayed:
#             status = "partially_extracted"
#         else:
#             status = "available"
#     return {"items": clean, "status": status, "total_displayed": displayed,
#             "extracted": extracted, "valid": valid_n, "rejected": rejected_n,
#             "missing_ratings": missing_ratings, "warnings": warnings[:20]}

# def normalize_native_item(it: dict) -> dict:
#     def pick(*keys, default="N/A"):
#         for k in keys:
#             v = it.get(k)
#             if v not in (None, "", []):
#                 return v
#         return default

#     star = pick("starNum", "star", "score", "rating", "ratingStar", "feedbackStar", default=None)
#     media = (it.get("feedbackMedia") or it.get("mediaList") or it.get("images")
#              or it.get("imageList") or [])
#     imgs, vids = [], []
#     for mo in media if isinstance(media, list) else []:
#         if isinstance(mo, dict):
#             u = mo.get("url") or mo.get("src") or mo.get("imageUrl") or mo.get("videoUrl")
#         else:
#             u = mo
#         if not isinstance(u, str) or not u.startswith("http"):
#             continue
#         is_vid = (
#             isinstance(mo, dict)
#             and (mo.get("videoUrl") or str(mo.get("type", "")).lower() == "video")
#         ) or bool(re.search(r"\.(mp4|webm|mov|m3u8)(\?|$)", u, re.I))
#         (vids if is_vid else imgs).append(u)

#     text = str(pick("content", "feedback", "feedbackContent", "reviewContent", "comment", "translatedContent"))
#     reply = pick("sellerReply", "supplierReply", "replyContent", "reply",
#                  "feedbackReply", "companyReply", default=None)
#     if isinstance(reply, dict):
#         reply = reply.get("content") or reply.get("text") or reply.get("reply")
#     if reply is None:
#         text, reply = split_supplier_reply(text)
#     native_id = pick("feedbackId", "reviewId", "evaluateId", "id", default=None)
#     return {
#         # Alibaba's own review id when the feedback API exposes one; the mapper
#         # falls back to a content-derived id otherwise.
#         "review_id": str(native_id) if native_id is not None else None,
#         "reviewer": str(pick("buyerName", "memberName", "displayName", "nickName", "buyerNickName", "reviewerName")),
#         "country": str(pick("countryName", "country", "buyerCountry", "countryCode")),
#         "date": str(pick("gmtCreate", "postDate", "feedbackDate", "createTime", "date", "orderCompleteTime")),
#         "stars": str(star) if star is not None else "N/A",
#         "text": text,
#         "supplier_reply": (str(reply).strip() or None) if reply else None,
#         "model_number": sanitize_model_number(pick("sku", "goodsSku", "skuInfo", "specInfo")),
#         "verified_purchase": bool(it.get("verifiedPurchase") or it.get("isVerified") or it.get("hasOrder")),
#         "repeat_buyer": bool(it.get("repeatBuyer") or it.get("isRepeat")),
#         "media": {"images": dedupe(imgs), "videos": dedupe(vids)},
#     }


# # ── Amazon-aligned helpers (pure; used by engine, mapper and adapter) ─────────
# # Same rule as the Amazon parsers: never fabricate. Anything the page does not
# # expose stays None / empty.

# def _blank(v) -> bool:
#     return v is None or str(v).strip().lower() in _BAD_NAMES


# def to_int(raw) -> int | None:
#     if raw is None or isinstance(raw, bool):
#         return None
#     if isinstance(raw, (int, float)):
#         return int(raw)
#     m = re.search(r"\d[\d,]*", str(raw))
#     return int(m.group().replace(",", "")) if m else None


# def parse_rating_value(raw) -> float | None:
#     """Average rating as a float on the 1-5 scale (4.8 stays 4.8; ``parse_stars``
#     rounds, which is right for single reviews but not for averages)."""
#     if raw is None or isinstance(raw, bool) or str(raw).strip() in ("", "N/A"):
#         return None
#     if isinstance(raw, (int, float)):
#         v = float(raw)
#     else:
#         m = re.search(r"\d+(?:\.\d+)?", str(raw))
#         if not m:
#             return None
#         v = float(m.group())
#     return round(v, 1) if 0 < v <= 5 else None


# def video_urls(videos) -> list[str]:
#     """Detail-page videos are dicts (``{"url": ..., "cover_url": ...}``) while
#     search-card videos are plain URLs; return URL strings only."""
#     out = []
#     for v in videos or []:
#         u = v.get("url") if isinstance(v, dict) else v
#         if isinstance(u, str):
#             u = "https:" + u if u.startswith("//") else u
#             if u.startswith("http"):
#                 out.append(u)
#     return dedupe(out)


# def currency_from_pattern(pattern) -> str | None:
#     """ISO code from Alibaba's ``currencyRule.currencyPattern`` (e.g. ``US$#``)."""
#     if not pattern:
#         return None
#     for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
#         if sym in str(pattern):
#             return _CURRENCY_SYMBOLS[sym]
#     return None


# def _is_number(v) -> bool:
#     return isinstance(v, (int, float)) and not isinstance(v, bool)


# def resolve_price(price_range_raw, ex: dict | None) -> dict:
#     """Product price in the display currency: the card's price text, refined by
#     the detail page's quantity tiers when present. ``usd_min``/``usd_max`` are
#     Alibaba's own US-dollar figures (``dollarPrice``), never a conversion."""
#     ex = ex or {}
#     price = parse_price(price_range_raw)
#     tier_prices = [t["price"] for t in ex.get("price_tiers") or []
#                    if isinstance(t, dict) and _is_number(t.get("price"))]
#     price["price_source"] = "card" if price["price_min"] is not None else "none"
#     if tier_prices:
#         price["price_min"], price["price_max"] = min(tier_prices), max(tier_prices)
#         price["price_source"] = "price_tiers"
#         if not price.get("currency"):
#             price["currency"] = currency_from_pattern(ex.get("price_currency_pattern"))
#     price["usd_min"] = ex.get("price_usd_min") if _is_number(ex.get("price_usd_min")) else None
#     price["usd_max"] = ex.get("price_usd_max") if _is_number(ex.get("price_usd_max")) else None
#     price["price_unit"] = price.get("price_unit") or ex.get("price_unit")
#     return price


# def spec_get(specs: dict, *names: str):
#     """Case-insensitive exact key lookup; first non-blank value wins."""
#     lower = {str(k).strip().lower(): v for k, v in (specs or {}).items()}
#     for n in names:
#         v = lower.get(n.lower())
#         if not _blank(v):
#             return str(v).strip()
#     return None


# def variant_options(variants) -> dict[str, list[dict]]:
#     """SKU attributes in the Amazon/Flipkart ``variants`` shape:
#     ``{"Color": [{"option": "Black", "url": None, "product_id": None}]}``.
#     Alibaba variants share one offer, so they have no own URL / product id."""
#     out: dict[str, list[dict]] = {}
#     for v in variants or []:
#         if not isinstance(v, dict):
#             continue
#         name = re.sub(r"\s+", " ", str(v.get("name") or "")).strip()
#         values = dedupe(re.sub(r"\s+", " ", x).strip() for x in v.get("values") or [] if isinstance(x, str))
#         if name and values:
#             opts = out.setdefault(name, [])
#             for val in values:
#                 if all(o["option"] != val for o in opts):
#                     opts.append({"option": val, "url": None, "product_id": None})
#     return out


# _CRUMB_NOISE = {"home", "all categories", "all industries", "alibaba.com", "alibaba", "products"}


# def clean_breadcrumbs(path) -> list[str]:
#     """Category path from the detail page's SEO breadcrumb, without site-level
#     entries such as "Home" / "All Industries"."""
#     out: list[str] = []
#     for c in path or []:
#         c = re.sub(r"\s+", " ", str(c or "")).strip()
#         if c and c.lower() not in _CRUMB_NOISE and c not in out:
#             out.append(c)
#     return out


# _MFG_SPEC_KEYS = {
#     "manufactured_by": ("manufacturer", "manufacturer name", "manufactured by"),
#     "country_of_origin": ("place of origin", "country of origin", "origin"),
#     "brand_owner": ("brand owner",),
# }


# def extract_manufacturer(ex: dict | None, brand: str | None = None) -> dict | None:
#     """Manufacturer details in the Amazon/Flipkart ``manufacturer`` shape, read
#     only from the product's own attributes (the supplier's business type is
#     not taken as proof that it made this product)."""
#     ex = ex or {}
#     specs = ex.get("specifications") or {}
#     out = {}
#     for key, names in _MFG_SPEC_KEYS.items():
#         v = spec_get(specs, *names)
#         if key == "country_of_origin" and not v and not _blank(ex.get("place_of_origin")):
#             v = str(ex["place_of_origin"]).strip()
#         if v:
#             out[key] = v
#     if not out:
#         return None
#     name = out.get("manufactured_by") or brand
#     if name:
#         out["name"] = name
#     return out


# def wholesale_terms(ex: dict | None, card_moq) -> dict | None:
#     """Alibaba's B2B purchase terms (MOQ, quantity price tiers, lead time,
#     customization, shipping origin, after-sales / returns) for
#     ``commercial.offers_detail``. None when the page shows none of them."""
#     ex = ex or {}
#     moq = parse_moq(card_moq)
#     if _is_number(ex.get("moq_value")):
#         moq["moq_value"] = ex["moq_value"]
#         moq["moq_extraction_status"] = "parsed"
#         if not moq["moq_unit"] and ex.get("price_unit"):
#             moq["moq_unit"] = str(ex["price_unit"]).lower()
#     custom = ex.get("customization") or {}
#     terms = {
#         "moq": moq if moq["moq_value"] is not None or moq["moq_raw"] else None,
#         "price_tiers": [t for t in ex.get("price_tiers") or [] if isinstance(t, dict)],
#         "lead_time": [t for t in ex.get("lead_time") or [] if isinstance(t, dict)],
#         "box_moq": ex.get("box_moq"),
#         "shipping_origin": ex.get("shipping_origin"),
#         "customization": custom if (custom.get("light") or custom.get("fast") or custom.get("items")) else None,
#         "after_sales_service": None if _blank(ex.get("after_sales_service")) else ex["after_sales_service"],
#         "returns": None if _blank(ex.get("returns")) else ex["returns"],
#     }
#     terms = {k: v for k, v in terms.items() if v not in (None, "", [], {})}
#     return terms or None


# def build_review_summary(detail: dict | None, card_rating=None, product_url: str | None = None) -> dict:
#     """Review aggregate in Amazon's ``review_summary`` shape (same keys, same
#     meaning), followed by Alibaba-only keys: the review source used and the
#     supplier-level (store) review aggregate. Values the page does not show
#     stay None / empty."""
#     detail = detail or {}
#     prns = detail.get("product_review_summary") or {}
#     pr = detail.get("product_reviews") or {}
#     sr = detail.get("store_reviews") or {}
#     srs = detail.get("store_review_summary") or {}
#     seller = detail.get("seller") or {}

#     total = to_int(prns.get("total_review_count"))
#     if total is None and pr.get("tab_count"):
#         total = to_int(pr.get("tab_count"))
#     avg = parse_rating_value(prns.get("average_star"))
#     if avg is None:
#         avg = parse_rating_value(card_rating)

#     if pr:
#         stop = pr.get("stop_reason") or "first review page only"
#     else:
#         stop = "product has no reviews" if total == 0 else "no product reviews found on page"

#     store = None
#     if sr or srs:
#         store = {
#             "average_rating": parse_rating_value(srs.get("averageStar")),
#             "total_reviews": to_int(srs.get("total_reviews")) or to_int(sr.get("tab_count")),
#             "sub_ratings": srs.get("sub_ratings") or {},
#             "feedback_url": seller.get("feedbackUrl"),
#             "pages_scraped": sr.get("feedback_pages") or sr.get("pages_scraped") or (1 if sr else 0),
#             "stop_reason": sr.get("feedback_termination") or sr.get("stop_reason"),
#             "complete": sr.get("feedback_complete"),
#             "review_source": sr.get("_source"),
#         }

#     summary = {
#         "total_ratings": None,  # Alibaba reports a review count, not a separate ratings count
#         "total_reviews": total,
#         "average_rating": avg,
#         "rating_breakdown": {},
#         "rating_breakdown_percent": {},
#         "positive_feedback_percent": None,
#         "negative_feedback_percent": None,
#         "aspect_ratings": {},
#         "customers_say": None,
#         "review_url": product_url,  # Alibaba shows product reviews on the product page
#         "pages_scraped": pr.get("pages_scraped", 1) if pr else 0,
#         "stop_reason": stop,
#         "sort_used": "default",  # Alibaba exposes no review-order control
#         "login_required": False,
#         "review_source": pr.get("_source"),
#         "review_tab": pr.get("tab_label"),
#         "store_reviews": store,
#     }
#     if detail.get("error"):
#         summary["error"] = detail["error"]
#     return summary

from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_BAD_NAMES = {"", "n/a", "na", "null", "none", "-", "unknown"}

def product_id_from_href(href: str) -> str:
    m = re.search(r"_(\d{8,})(?:\.html|$|\?)", href)
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    pid = (qs.get("productId") or qs.get("offerId") or [""])[0]
    if pid:
        return pid
    clean = href.split("?")[0].rstrip("/")
    return hashlib.md5(clean.encode()).hexdigest()[:16]

def _norm_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())

def resolve_supplier_identity(company_id, card_name, detail_name) -> dict:
    real_name = detail_name if _norm_name(detail_name) not in _BAD_NAMES else card_name
    norm = _norm_name(real_name)

    cid = None
    if company_id not in (None, "", 0, "0"):
        try:
            cid = int(company_id)
        except (TypeError, ValueError):
            cid = str(company_id)

    if cid is not None:
        supplier_id = f"ali-{cid}"
    elif norm not in _BAD_NAMES:
        supplier_id = "name-" + hashlib.md5(norm.encode()).hexdigest()[:12]
    else:
        supplier_id = None

    return {
        "supplier_id": supplier_id,
        "company_id": cid,
        "supplier_name": real_name if norm not in _BAD_NAMES else None,
        "raw": {"card_name": card_name, "detail_name": detail_name},
    }

_DATE_FORMATS = (
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
)

# Time-of-day / zone suffix some sources append to a date, e.g. Alibaba's
# supplier feedback "12 Nov 2026 01:06" or an API "2026-11-12 01:06:00".
_TIME_SUFFIX_RE = re.compile(
    r"[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:[AaPp][Mm])?"
    r"(?:\s*(?:Z|UTC|GMT|[+-]\d{2}:?\d{2}))?\s*$"
)

def parse_posted_date(s: str | None):
    if not s or s == "N/A":
        return None
    s = re.sub(r"(?i)^\s*(posted|updated|published)[:\s]+", "", s.strip())
    s = _TIME_SUFFIX_RE.sub("", s).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])).date()
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        try:
            return datetime.strptime(f"{m[1][:3]} {m[2]} {m[3]}", "%b %d %Y").date()
        except ValueError:
            return None
    return None

_CURRENCY_SYMBOLS = {
    "US$": "USD", "C$": "CAD", "A$": "AUD", "₹": "INR", "$": "USD",
    "€": "EUR", "£": "GBP", "¥": "CNY", "₫": "VND", "₩": "KRW",
}
_CURRENCY_WORDS = {
    "rs": "INR", "inr": "INR", "usd": "USD", "cny": "CNY",
    "rmb": "CNY", "eur": "EUR", "gbp": "GBP",
}

def _to_decimal(num: str):
    try:
        return Decimal(num.replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None

def dec_num(d):
    if not isinstance(d, Decimal):
        return None
    return int(d) if d == d.to_integral_value() else float(d)

_CURRENCY_MARKER_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True))
)
_DISCOUNT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*off\b", re.I)
# A two-decimal price run straight into its discount badge ("256.16" + "10% off"
# read back as "256.1610% off" when sibling nodes were concatenated).
_GLUED_DISCOUNT_RE = re.compile(r"(\d[\d,]*\.\d{2})(\d{1,2})(\s*%\s*off\b)", re.I)
_RANGE_END_RE = re.compile(r"(?:[-–—]|\bto)\s*$", re.I)


def _price_segments(text: str) -> list[str]:
    """Split a card price at each currency symbol, keeping "$1.20-$2.50" (a
    range that repeats the symbol) together as one segment."""
    starts = [m.start() for m in _CURRENCY_MARKER_RE.finditer(text)]
    if len(starts) < 2:
        return [text]
    cuts = [0 if starts[0] == 0 else starts[0]] + starts[1:] + [len(text)]
    segs: list[str] = []
    for a, b in zip(cuts, cuts[1:]):
        seg = text[a:b]
        if segs and _RANGE_END_RE.search(segs[-1].strip()):
            segs[-1] += seg
        else:
            segs.append(seg)
    return segs


def _range_of(text: str):
    nums = [d for d in (_to_decimal(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)) if d is not None]
    if not nums:
        return None, None, False
    if len(nums) >= 2 and re.search(r"[-–—]|\bto\b", text):
        return min(nums), max(nums), True
    return nums[0], nums[0], False


def parse_price(raw) -> dict:
    """Card price text -> current price range, plus the struck-through original
    price and the "N% off" badge when the card shows them
    (``"₹196.39-230.55 ₹218.22-256.16 10% off"``). Numbers that belong to the
    original price or the discount are never mixed into the current range."""
    out = {
        "price_raw": raw if raw not in (None, "", "N/A") else None,
        "price_min": None, "price_max": None, "currency": None, "price_unit": None,
        "original_min": None, "original_max": None, "discount_percentage": None,
        "price_extraction_status": "missing",
    }
    if not raw or str(raw).strip() in ("", "N/A"):
        return out
    text = _GLUED_DISCOUNT_RE.sub(r"\1 \2\3", str(raw).strip())

    for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in text:
            out["currency"] = _CURRENCY_SYMBOLS[sym]
            break
    if out["currency"] is None:
        wm = re.search(r"\b(rs|inr|usd|cny|rmb|eur|gbp)\b", text, re.I)
        if wm:
            out["currency"] = _CURRENCY_WORDS[wm.group(1).lower()]

    um = re.search(r"/\s*([A-Za-z][A-Za-z ]{0,15})", text)
    if um:
        out["price_unit"] = um.group(1).strip().lower()

    dm = _DISCOUNT_RE.search(text)
    if dm:
        pct = float(dm.group(1))
        if 0 < pct < 100:
            out["discount_percentage"] = int(pct) if pct == int(pct) else pct
        text = (text[:dm.start()] + " " + text[dm.end():]).strip()

    segs = _price_segments(text)
    lo, hi, is_range = _range_of(segs[0])
    if lo is None:
        out["price_extraction_status"] = "unparseable"
        return out
    out["price_min"], out["price_max"] = dec_num(lo), dec_num(hi)
    out["price_extraction_status"] = "range_parsed" if is_range else "parsed"

    if len(segs) >= 2:
        olo, ohi, _ = _range_of(segs[1])
        # Only a higher price can be the struck-through one.
        if olo is not None and olo >= lo and ohi >= hi:
            out["original_min"], out["original_max"] = dec_num(olo), dec_num(ohi)
    return out

_UNIT_SINGULAR = {
    "pieces": "piece", "pcs": "piece", "pc": "piece", "sets": "set", "units": "unit",
    "kilograms": "kilogram", "kgs": "kilogram", "kg": "kilogram", "tons": "ton",
    "tonnes": "tonne", "meters": "meter", "metres": "meter", "pairs": "pair",
    "boxes": "box", "bags": "bag", "rolls": "roll", "cartons": "carton",
    "dozens": "dozen", "acres": "acre", "liters": "liter", "litres": "liter",
}

def parse_moq(raw) -> dict:
    out = {
        "moq_raw": raw if raw not in (None, "", "N/A") else None,
        "moq_value": None, "moq_unit": None, "moq_extraction_status": "missing",
    }
    if not raw or str(raw).strip() in ("", "N/A"):
        return out
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]+)", str(raw))
    if not m:
        out["moq_extraction_status"] = "unparseable"
        return out
    val = _to_decimal(m.group(1))
    if val is None:
        out["moq_extraction_status"] = "unparseable"
        return out
    out["moq_value"] = dec_num(val)
    unit = m.group(2).lower()
    out["moq_unit"] = _UNIT_SINGULAR.get(unit, unit)
    out["moq_extraction_status"] = "parsed"
    return out

_ICON_RE = re.compile(
    r"\.svg(\?|$)|/flags?/|/common/|/mobile/|tps-\d{1,3}-\d{1,3}\.|"
    r"-55-tps-|-56-tps-|icon|sprite|placeholder|avatar",
    re.I,
)

def _is_icon(u: str) -> bool:
    return not u or not u.startswith("http") or bool(_ICON_RE.search(u))

def classify_images(card_images, product_media, supplier_logo) -> dict:
    pm_imgs = (product_media or {}).get("images") or []
    product_imgs: list[str] = []
    seen: set[str] = set()
    for u in pm_imgs:
        if u and u not in seen and not _is_icon(u):
            seen.add(u)
            product_imgs.append(u)
    if not product_imgs:
        for u in card_images or []:
            if u and u not in seen and not _is_icon(u):
                seen.add(u)
                product_imgs.append(u)
    other: list[str] = []
    for u in card_images or []:
        if u and u not in seen and not _is_icon(u) and u != supplier_logo:
            seen.add(u)
            other.append(u)
    if product_imgs:
        status = "classified"
    elif card_images:
        status = "icons_only"
    else:
        status = "missing"
    return {
        "main_image_url": product_imgs[0] if product_imgs else None,
        "product_image_urls": product_imgs,
        "supplier_logo_url": supplier_logo or None,
        "other_media_urls": other,
        "image_extraction_status": status,
    }

_MODEL_JUNK = {
    "number", "no", "no.", "num", "num.", "model", "model number",
    "n/a", "na", "-", "null", "none", "",
}
_MODEL_LABELS = ("model number", "model no.", "model no", "model")
_CPU_LIKE_RE = re.compile(
    r"^(i[3579][\s-]?\d{2,5}[a-z]*|celeron|pentium|atom|ryzen|athlon|"
    r"core\s*(i|ultra|m)|xeon|snapdragon|mediatek|mt\d|"
    r"n\d{3,4}[a-z]*|j\d{3,4}[a-z]*|a\d{3,4}[a-z]*)$",
    re.I,
)
_GENERIC_BRANDS = {
    "oem", "odm", "oem/odm", "none", "n/a", "na", "null", "-", "generic",
    "no brand", "nobrand", "unbranded", "other", "others", "customized", "custom", "",
}

def sanitize_model_number(v):
    if v in (None, "", "N/A"):
        return None
    s = str(v).strip()
    if s.lower() in _MODEL_JUNK:
        return None
    return s

def _norm_ident(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()

def _cpu_spec_values(specs: dict) -> list[str]:
    out = []
    for k, v in (specs or {}).items():
        kl = str(k).lower()
        if ("processor" in kl or "cpu" in kl) and v not in (None, "", "N/A"):
            out.append(str(v))
    return out

def _model_candidates(ex: dict) -> list[tuple[str, str]]:
    cands: list[tuple[str, str]] = []
    for v in ex.get("variants") or []:
        if _norm_ident(v.get("name")) in _MODEL_LABELS:
            vals = [x for x in (v.get("values") or []) if x]
            if vals:
                cands.append(("variant", str(vals[0]).strip()))
                break
    for k, val in (ex.get("specifications") or {}).items():
        if _norm_ident(k) in _MODEL_LABELS and val not in (None, "", "N/A"):
            cands.append(("specification", str(val).strip()))
            break
    return cands

def _is_cpu_value(model: str, cpu_values: list[str]) -> str | None:
    nm = _norm_ident(model)
    if not nm:
        return None
    for cv in cpu_values:
        if _norm_ident(cv) == nm:
            return cv
    if _CPU_LIKE_RE.match(model.strip()) and " " not in nm:
        for cv in cpu_values:
            toks = set(re.findall(r"[a-z0-9][a-z0-9\-]*", cv.lower()))
            if nm in toks and _norm_ident(cv) != nm:
                return cv
    return None

def validate_model_number(ex: dict) -> dict:
    specs = ex.get("specifications") or {}
    cpu_values = _cpu_spec_values(specs)
    raw_cands = _model_candidates(ex)

    cleaned: list[tuple[str, str]] = []
    for src, val in raw_cands:
        s = sanitize_model_number(val)
        if s is not None:
            cleaned.append((src, s))

    if not cleaned:
        return {"value": None, "status": "not_provided", "source": None, "raw": None,
                "candidates": [], "reason": "no model-number attribute on page",
                "collides_with": None, "confidence": None}

    all_values = [c for _, c in cleaned]
    collisions = [(src, val, _is_cpu_value(val, cpu_values)) for src, val in cleaned]
    non_cpu = [(src, val) for (src, val, col) in collisions if col is None]

    if not non_cpu:
        first_src, first_val, first_col = collisions[0]
        return {"value": None, "status": "invalid", "source": first_src, "raw": first_val,
                "candidates": all_values,
                "reason": f"candidate {first_val!r} equals processor/CPU value",
                "collides_with": first_col, "confidence": None}

    top_src, top_val, top_col = collisions[0]
    if top_col is not None:
        return {"value": None, "status": "not_verified", "source": top_src, "raw": top_val,
                "candidates": all_values,
                "reason": f"authoritative model source was a CPU value ({top_val!r})",
                "collides_with": top_col, "confidence": None}

    distinct = {_norm_ident(v) for _, v in non_cpu}
    status = "conflicting" if len(distinct) > 1 else "available"
    if status == "conflicting":
        confidence = "low"
    else:
        confidence = "high" if top_src == "variant" else "medium"
    reason = (f"multiple distinct model candidates {[v for _, v in non_cpu]}"
              if status == "conflicting" else None)
    return {"value": top_val, "status": status, "source": top_src, "raw": top_val,
            "candidates": all_values, "reason": reason, "collides_with": None,
            "confidence": confidence}

def sanitize_brand(v) -> dict:
    if v in (None, "", "N/A"):
        return {"value": None, "status": "missing_on_source", "raw": None,
                "reason": "no brand attribute on page"}
    s = str(v).strip()
    if s.lower() in _GENERIC_BRANDS:
        return {"value": None, "status": "not_verified", "raw": s,
                "reason": f"generic value {s!r}; consumer brand not established"}
    return {"value": s, "status": "available", "raw": s, "reason": None}

_UI_NOISE_RE = re.compile(
    r"^(what are you looking for|all categories|know your supplier|search|sort by|"
    r"based on\b|most relevant|related searches|frequently bought|you may|about this|"
    r"product details|see product details|helpful|verified purchase|repeat buyer|"
    r"get the app|sign in|join free|trade assurance|categories|home|"
    r"previous slide|next slide|prev slide|previous|next|slide \d+|"
    r"min\.?\s*order|minimum order|moq\b|min\.? order quantity|"
    r"payment|shipping|logistics|add to cart|contact supplier|chat now|"
    r"view (more|all)|show (more|original)|report|share|save)\b",
    re.I,
)
_TAGBAR_RE = re.compile(r"^(?:[\w ]+\(\d+\))(?:\s*[\w ]+\(\d+\)){1,}$")
_REPLY_RE = re.compile(r"(?i)supplier'?s?\s*repl(?:y|ies)\s*:?\s*")

def split_supplier_reply(text):
    if not isinstance(text, str):
        return text, None
    m = _REPLY_RE.search(text)
    if not m:
        return text, None
    customer = text[: m.start()].strip() or "N/A"
    reply = text[m.end():].strip() or None
    return customer, reply

def _looks_like_ui(text) -> bool:
    t = str(text or "").strip()
    if not t or t == "N/A":
        return False
    return bool(_UI_NOISE_RE.match(t)) or bool(_TAGBAR_RE.match(t))

def parse_stars(raw):
    if raw in (None, "", "N/A"):
        return None
    if isinstance(raw, (int, float)):
        v = round(float(raw))
        return v if 1 <= v <= 5 else None
    m = re.search(r"([0-5](?:\.\d)?)", str(raw))
    if not m:
        return None
    v = round(float(m.group(1)))
    return v if 1 <= v <= 5 else None

def dedupe(seq) -> list:
    seen, out = set(), []
    for x in seq or []:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def validate_review(item: dict) -> dict:
    warnings: list[str] = []
    reviewer = item.get("reviewer", "N/A")
    country = item.get("country", "N/A")
    rating = parse_stars(
        item.get("rating") if item.get("rating") is not None else item.get("stars")
    )

    ctext, _ = split_supplier_reply(item.get("text", "N/A"))
    media = item.get("media") or item.get("review_media") or {}
    media_present = bool(media.get("images") or media.get("videos"))

    clean_reviewer = None
    if _looks_like_ui(reviewer):
        warnings.append(f"reviewer looks like UI text: {reviewer!r}")
    elif reviewer not in (None, "", "N/A"):
        clean_reviewer = reviewer

    clean_country = None
    if _looks_like_ui(country):
        warnings.append(f"country looks like UI text: {country!r}")
    elif country not in (None, "", "N/A"):
        clean_country = country

    text_is_ui = _looks_like_ui(ctext)
    if text_is_ui:
        warnings.append(f"review text looks like UI/page content: {str(ctext)[:40]!r}")

    genuine_text = ctext not in (None, "", "N/A") and not text_is_ui
    has_content = (
        genuine_text
        or (rating is not None)
        or (media_present and clean_reviewer is not None)
    )
    return {"valid": bool(has_content), "rating": rating, "warnings": warnings,
            "clean_reviewer": clean_reviewer, "clean_country": clean_country}

def review_quality(block: dict | None, tab_count: int, complete: bool = False) -> dict:
    if block is None:
        status = "no_reviews_reported" if tab_count == 0 else "unavailable"
        return {"items": [], "status": status, "total_displayed": tab_count,
                "extracted": 0, "valid": 0, "rejected": 0, "missing_ratings": 0,
                "warnings": []}

    raw = block.get("reviews", []) or []
    clean, warnings = [], []
    rejected_n = 0
    for it in raw:
        res = validate_review(it)
        warnings.extend(res["warnings"])
        if not res["valid"]:
            rejected_n += 1
            continue
        it = dict(it)
        it["reviewer"] = res["clean_reviewer"] or "N/A"
        it["country"] = res["clean_country"] or "N/A"
        it["rating"] = res["rating"]
        it["valid"] = True
        clean.append(it)

    extracted = valid_n = len(clean)
    missing_ratings = sum(1 for it in clean if it.get("rating") is None)

    if complete:
        displayed = valid_n
        status = "available" if valid_n > 0 else "no_reviews_reported"
    else:
        displayed = max(tab_count, valid_n) if tab_count > 0 else valid_n
        if displayed == 0:
            status = "no_reviews_reported"
        elif valid_n < displayed:
            status = "partially_extracted"
        else:
            status = "available"
    return {"items": clean, "status": status, "total_displayed": displayed,
            "extracted": extracted, "valid": valid_n, "rejected": rejected_n,
            "missing_ratings": missing_ratings, "warnings": warnings[:20]}

def normalize_native_item(it: dict) -> dict:
    def pick(*keys, default="N/A"):
        for k in keys:
            v = it.get(k)
            if v not in (None, "", []):
                return v
        return default

    star = pick("starNum", "star", "score", "rating", "ratingStar", "feedbackStar", default=None)
    media = (it.get("feedbackMedia") or it.get("mediaList") or it.get("images")
             or it.get("imageList") or [])
    imgs, vids = [], []
    for mo in media if isinstance(media, list) else []:
        if isinstance(mo, dict):
            u = mo.get("url") or mo.get("src") or mo.get("imageUrl") or mo.get("videoUrl")
        else:
            u = mo
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        is_vid = (
            isinstance(mo, dict)
            and (mo.get("videoUrl") or str(mo.get("type", "")).lower() == "video")
        ) or bool(re.search(r"\.(mp4|webm|mov|m3u8)(\?|$)", u, re.I))
        (vids if is_vid else imgs).append(u)

    text = str(pick("content", "feedback", "feedbackContent", "reviewContent", "comment", "translatedContent"))
    reply = pick("sellerReply", "supplierReply", "replyContent", "reply",
                 "feedbackReply", "companyReply", default=None)
    if isinstance(reply, dict):
        reply = reply.get("content") or reply.get("text") or reply.get("reply")
    if reply is None:
        text, reply = split_supplier_reply(text)
    native_id = pick("feedbackId", "reviewId", "evaluateId", "id", default=None)
    return {
        # Alibaba's own review id when the feedback API exposes one; the mapper
        # falls back to a content-derived id otherwise.
        "review_id": str(native_id) if native_id is not None else None,
        "reviewer": str(pick("buyerName", "memberName", "displayName", "nickName", "buyerNickName", "reviewerName")),
        "country": str(pick("countryName", "country", "buyerCountry", "countryCode")),
        "date": str(pick("gmtCreate", "postDate", "feedbackDate", "createTime", "date", "orderCompleteTime")),
        "stars": str(star) if star is not None else "N/A",
        "text": text,
        "supplier_reply": (str(reply).strip() or None) if reply else None,
        "model_number": sanitize_model_number(pick("sku", "goodsSku", "skuInfo", "specInfo")),
        "verified_purchase": bool(it.get("verifiedPurchase") or it.get("isVerified") or it.get("hasOrder")),
        "repeat_buyer": bool(it.get("repeatBuyer") or it.get("isRepeat")),
        "media": {"images": dedupe(imgs), "videos": dedupe(vids)},
    }


# ── Amazon-aligned helpers (pure; used by engine, mapper and adapter) ─────────
# Same rule as the Amazon parsers: never fabricate. Anything the page does not
# expose stays None / empty.

def _blank(v) -> bool:
    return v is None or str(v).strip().lower() in _BAD_NAMES


def to_int(raw) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    m = re.search(r"\d[\d,]*", str(raw))
    return int(m.group().replace(",", "")) if m else None


def parse_rating_value(raw) -> float | None:
    """Average rating as a float on the 1-5 scale (4.8 stays 4.8; ``parse_stars``
    rounds, which is right for single reviews but not for averages)."""
    if raw is None or isinstance(raw, bool) or str(raw).strip() in ("", "N/A"):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        m = re.search(r"\d+(?:\.\d+)?", str(raw))
        if not m:
            return None
        v = float(m.group())
    return round(v, 1) if 0 < v <= 5 else None


def video_urls(videos) -> list[str]:
    """Detail-page videos are dicts (``{"url": ..., "cover_url": ...}``) while
    search-card videos are plain URLs; return URL strings only."""
    out = []
    for v in videos or []:
        u = v.get("url") if isinstance(v, dict) else v
        if isinstance(u, str):
            u = "https:" + u if u.startswith("//") else u
            if u.startswith("http"):
                out.append(u)
    return dedupe(out)


def currency_from_pattern(pattern) -> str | None:
    """ISO code from Alibaba's ``currencyRule.currencyPattern`` (e.g. ``US$#``)."""
    if not pattern:
        return None
    for sym in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in str(pattern):
            return _CURRENCY_SYMBOLS[sym]
    return None


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve_price(price_range_raw, ex: dict | None) -> dict:
    """Product price in the display currency: the card's price text, refined by
    the detail page's quantity tiers when present. ``usd_min``/``usd_max`` are
    Alibaba's own US-dollar figures (``dollarPrice``), never a conversion."""
    ex = ex or {}
    price = parse_price(price_range_raw)
    tier_prices = [t["price"] for t in ex.get("price_tiers") or []
                   if isinstance(t, dict) and _is_number(t.get("price"))]
    price["price_source"] = "card" if price["price_min"] is not None else "none"
    if tier_prices:
        price["price_min"], price["price_max"] = min(tier_prices), max(tier_prices)
        price["price_source"] = "price_tiers"
        if not price.get("currency"):
            price["currency"] = currency_from_pattern(ex.get("price_currency_pattern"))
    price["usd_min"] = ex.get("price_usd_min") if _is_number(ex.get("price_usd_min")) else None
    price["usd_max"] = ex.get("price_usd_max") if _is_number(ex.get("price_usd_max")) else None
    price["price_unit"] = price.get("price_unit") or ex.get("price_unit")
    return price


def spec_get(specs: dict, *names: str):
    """Case-insensitive exact key lookup; first non-blank value wins."""
    lower = {str(k).strip().lower(): v for k, v in (specs or {}).items()}
    for n in names:
        v = lower.get(n.lower())
        if not _blank(v):
            return str(v).strip()
    return None


def variant_options(variants) -> dict[str, list[dict]]:
    """SKU attributes in the Amazon/Flipkart ``variants`` shape:
    ``{"Color": [{"option": "Black", "url": None, "product_id": None}]}``.
    Alibaba variants share one offer, so they have no own URL / product id."""
    out: dict[str, list[dict]] = {}
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        name = re.sub(r"\s+", " ", str(v.get("name") or "")).strip()
        values = dedupe(re.sub(r"\s+", " ", x).strip() for x in v.get("values") or [] if isinstance(x, str))
        if name and values:
            opts = out.setdefault(name, [])
            for val in values:
                if all(o["option"] != val for o in opts):
                    opts.append({"option": val, "url": None, "product_id": None})
    return out


_CRUMB_NOISE = {"home", "all categories", "all industries", "alibaba.com", "alibaba", "products"}


def clean_breadcrumbs(path) -> list[str]:
    """Category path from the detail page's SEO breadcrumb, without site-level
    entries such as "Home" / "All Industries"."""
    out: list[str] = []
    for c in path or []:
        c = re.sub(r"\s+", " ", str(c or "")).strip()
        if c and c.lower() not in _CRUMB_NOISE and c not in out:
            out.append(c)
    return out


_MFG_SPEC_KEYS = {
    "manufactured_by": ("manufacturer", "manufacturer name", "manufactured by"),
    "country_of_origin": ("place of origin", "country of origin", "origin"),
    "brand_owner": ("brand owner",),
}


def extract_manufacturer(ex: dict | None, brand: str | None = None) -> dict | None:
    """Manufacturer details in the Amazon/Flipkart ``manufacturer`` shape, read
    only from the product's own attributes (the supplier's business type is
    not taken as proof that it made this product)."""
    ex = ex or {}
    specs = ex.get("specifications") or {}
    out = {}
    for key, names in _MFG_SPEC_KEYS.items():
        v = spec_get(specs, *names)
        if key == "country_of_origin" and not v and not _blank(ex.get("place_of_origin")):
            v = str(ex["place_of_origin"]).strip()
        if v:
            out[key] = v
    if not out:
        return None
    name = out.get("manufactured_by") or brand
    if name:
        out["name"] = name
    return out


def wholesale_terms(ex: dict | None, card_moq) -> dict | None:
    """Alibaba's B2B purchase terms (MOQ, quantity price tiers, lead time,
    customization, shipping origin, after-sales / returns) for
    ``commercial.offers_detail``. None when the page shows none of them."""
    ex = ex or {}
    moq = parse_moq(card_moq)
    if _is_number(ex.get("moq_value")):
        moq["moq_value"] = ex["moq_value"]
        moq["moq_extraction_status"] = "parsed"
        if not moq["moq_unit"] and ex.get("price_unit"):
            moq["moq_unit"] = str(ex["price_unit"]).lower()
    custom = ex.get("customization") or {}
    terms = {
        "moq": moq if moq["moq_value"] is not None or moq["moq_raw"] else None,
        "price_tiers": [t for t in ex.get("price_tiers") or [] if isinstance(t, dict)],
        "lead_time": [t for t in ex.get("lead_time") or [] if isinstance(t, dict)],
        "box_moq": ex.get("box_moq"),
        "shipping_origin": ex.get("shipping_origin"),
        "customization": custom if (custom.get("light") or custom.get("fast") or custom.get("items")) else None,
        "after_sales_service": None if _blank(ex.get("after_sales_service")) else ex["after_sales_service"],
        "returns": None if _blank(ex.get("returns")) else ex["returns"],
    }
    terms = {k: v for k, v in terms.items() if v not in (None, "", [], {})}
    return terms or None


def build_review_summary(detail: dict | None, card_rating=None, product_url: str | None = None) -> dict:
    """Review aggregate in Amazon's ``review_summary`` shape (same keys, same
    meaning), followed by Alibaba-only keys: the review source used and the
    supplier-level (store) review aggregate. Values the page does not show
    stay None / empty."""
    detail = detail or {}
    prns = detail.get("product_review_summary") or {}
    pr = detail.get("product_reviews") or {}
    sr = detail.get("store_reviews") or {}
    srs = detail.get("store_review_summary") or {}
    seller = detail.get("seller") or {}

    total = to_int(prns.get("total_review_count"))
    if total is None and pr.get("tab_count"):
        total = to_int(pr.get("tab_count"))
    avg = parse_rating_value(prns.get("average_star"))
    if avg is None:
        avg = parse_rating_value(card_rating)

    if pr:
        stop = pr.get("stop_reason") or "first review page only"
    else:
        stop = "product has no reviews" if total == 0 else "no product reviews found on page"

    store = None
    if sr or srs:
        store = {
            "average_rating": parse_rating_value(srs.get("averageStar")),
            # The tab's displayed total ("Store reviews (944)") is the review count;
            # the rating-scatter sum is only a count of rated feedback.
            "total_reviews": to_int(sr.get("tab_count")) or to_int(srs.get("total_reviews")),
            "sub_ratings": srs.get("sub_ratings") or {},
            "feedback_url": seller.get("feedbackUrl"),
            "pages_scraped": sr.get("feedback_pages") or sr.get("pages_scraped") or (1 if sr else 0),
            "stop_reason": sr.get("feedback_termination") or sr.get("stop_reason"),
            "complete": sr.get("feedback_complete"),
            "review_source": sr.get("_source"),
        }

    summary = {
        "total_ratings": None,  # Alibaba reports a review count, not a separate ratings count
        "total_reviews": total,
        "average_rating": avg,
        "rating_breakdown": {},
        "rating_breakdown_percent": {},
        "positive_feedback_percent": None,
        "negative_feedback_percent": None,
        "aspect_ratings": {},
        "customers_say": None,
        "review_url": product_url,  # Alibaba shows product reviews on the product page
        "pages_scraped": pr.get("pages_scraped", 1) if pr else 0,
        "stop_reason": stop,
        "sort_used": "default",  # Alibaba exposes no review-order control
        "login_required": False,
        "review_source": pr.get("_source"),
        "review_tab": pr.get("tab_label"),
        "store_reviews": store,
    }
    if detail.get("error"):
        summary["error"] = detail["error"]
    return summary