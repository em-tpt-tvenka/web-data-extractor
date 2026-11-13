"""
BazaarVoice Reviews & Ratings API Module (Schema v2.0.0).
Fetches aggregate ratings, star distributions, aspect ratings, user reviews,
reviewer profiles, media, and brand/seller replies for Samsung products.
"""

import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Tuple, Optional, List

from config import BV_API_URL, BV_PASSKEY, DEFAULT_HEADERS, REQUEST_TIMEOUT
from models import build_review_v2, normalize_iso_utc

logger = logging.getLogger(__name__)


def _extract_language_and_country(locale: Optional[str], location: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Extracts ISO language code (e.g. 'en') and country code (e.g. 'IN') from BV fields."""
    lang = None
    country = None
    if locale and isinstance(locale, str):
        parts = locale.replace("-", "_").split("_")
        if parts[0]:
            lang = parts[0].lower()
        if len(parts) > 1 and len(parts[1]) == 2:
            country = parts[1].upper()
    if not country and location and isinstance(location, str):
        loc = location.strip()
        if len(loc) == 2 and loc.isalpha():
            country = loc.upper()
    return lang, country


def parse_bazaarvoice_response(
    data: Dict[str, Any],
    product_id: str,
    platform: str = "samsung",
    days_cutoff: Optional[int] = 365,
    seller_id: Optional[str] = None,
    parent_product_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Converts a raw Bazaarvoice API JSON payload into Schema v2.0.0 Review Details
    plus the earliest review timestamp observed.
    """
    if not isinstance(data, dict):
        return None, None

    all_results = data.get("Results", []) or []
    submission_times = [
        r.get("SubmissionTime") for r in all_results if isinstance(r, dict) and r.get("SubmissionTime")
    ]
    earliest_review_date: Optional[str] = min(submission_times) if submission_times else None

    now = datetime.now(timezone.utc)
    cutoff_datetime = (now - timedelta(days=days_cutoff)) if (days_cutoff is not None and days_cutoff > 0) else None

    filtered_reviews: List[Dict[str, Any]] = []
    for r in all_results:
        if not isinstance(r, dict):
            continue
        sub_str = r.get("SubmissionTime")
        if cutoff_datetime is not None:
            if not sub_str:
                continue
            try:
                clean_str = sub_str.replace("Z", "+00:00")
                sub_dt = datetime.fromisoformat(clean_str)
                if sub_dt.tzinfo is None:
                    sub_dt = sub_dt.replace(tzinfo=timezone.utc)
                if sub_dt >= cutoff_datetime:
                    filtered_reviews.append(r)
            except Exception:
                filtered_reviews.append(r)
        else:
            filtered_reviews.append(r)

    items: List[Dict[str, Any]] = []
    all_ratings: List[float] = []
    local_dist = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}

    for r in filtered_reviews:
        raw_rating = r.get("Rating")
        rating_val: Optional[float] = None
        if raw_rating is not None:
            try:
                f_val = float(raw_rating)
                all_ratings.append(f_val)
                rating_val = int(f_val) if f_val.is_integer() else round(f_val, 2)
                b_key = str(int(round(f_val)))
                if b_key in local_dist:
                    local_dist[b_key] += 1
            except (ValueError, TypeError):
                pass

        rev_id = str(r.get("Id")) if r.get("Id") is not None else None
        pid = r.get("ProductId") or product_id
        lang, country = _extract_language_and_country(r.get("ContentLocale"), r.get("UserLocation"))

        raw_badges = r.get("Badges") or {}
        badge_ids = list(raw_badges.keys()) if isinstance(raw_badges, dict) else []
        badge_order = r.get("BadgesOrder") or badge_ids
        clean_badges = [str(b).replace("_", " ").title() for b in badge_order if b]
        badge_low = " ".join(str(b).lower() for b in badge_order)
        is_verified = True if ("verified" in badge_low) else (False if badge_order else None)
        is_incentivized = True if ("incentiv" in badge_low or "sweepstakes" in badge_low) else False

        helpful = r.get("TotalPositiveFeedbackCount")
        helpful_votes = int(helpful) if isinstance(helpful, (int, float)) else None

        # Tags (pros / cons)
        tag_dims = r.get("TagDimensions") or {}
        pros: List[str] = []
        cons: List[str] = []
        if isinstance(tag_dims, dict):
            pros = list((tag_dims.get("Pros") or {}).get("Values") or [])
            cons = list((tag_dims.get("Cons") or {}).get("Values") or [])

        # Media (photos / videos)
        rev_images: List[Dict[str, Any]] = []
        for p in r.get("Photos") or []:
            if not isinstance(p, dict):
                continue
            sizes = p.get("Sizes") or {}
            img_url = (
                (sizes.get("large") or {}).get("Url")
                or (sizes.get("normal") or {}).get("Url")
                or p.get("Url")
            )
            if img_url:
                rev_images.append({"url": img_url, "caption": p.get("Caption")})

        rev_videos: List[Dict[str, Any]] = []
        for v in r.get("Videos") or []:
            if not isinstance(v, dict):
                continue
            v_url = v.get("VideoUrl") or v.get("Url")
            if v_url:
                rev_videos.append({
                    "url": v_url,
                    "thumbnail_url": v.get("VideoThumbnailUrl") or v.get("ThumbnailUrl"),
                })

        # Comments / ClientResponses
        comments: List[Dict[str, Any]] = []
        for idx, cr in enumerate(r.get("ClientResponses") or [], start=1):
            if not isinstance(cr, dict):
                continue
            resp_text = cr.get("Response")
            if not resp_text:
                continue
            dept = (cr.get("Department") or "").strip()
            author_name = cr.get("Name") or dept or "Brand Support"
            role = "brand" if (platform == "samsung" or "brand" in dept.lower() or "support" in dept.lower()) else "seller"
            comments.append({
                "comment_id": str(cr.get("ResponseId") or f"{rev_id or 'R'}_C{idx}"),
                "review_id": rev_id,
                "platform_product_id": pid,
                "author_role": role,
                "author_name": author_name,
                "comment_text": resp_text,
                "created_at": normalize_iso_utc(cr.get("Date")),
            })

        items.append({
            "review_id": rev_id,
            "platform_product_id": pid,
            "rating": rating_val,
            "title": r.get("Title"),
            "body": r.get("ReviewText"),
            "published_at": normalize_iso_utc(r.get("SubmissionTime")),
            "language": lang,
            "is_verified_purchase": is_verified,
            "is_incentivized_or_default": is_incentivized,
            "is_recommended": r.get("IsRecommended"),
            "helpful_votes": helpful_votes,
            "reviewer": {
                "user_id": r.get("AuthorId"),
                "name": r.get("UserNickname"),
                "country_code": country,
                "badges": clean_badges,
            },
            "purchased_variant": {
                "variant_id": pid if pid != product_id else None,
                "attributes_text": None,
            },
            "tags": {
                "pros": pros,
                "cons": cons,
            },
            "media": {
                "images": rev_images,
                "videos": rev_videos,
            },
            "comments": comments,
        })

    # Product-level ReviewStatistics from Bazaarvoice Includes
    products_includes = (data.get("Includes") or {}).get("Products") or {}
    prod_info = products_includes.get(product_id, {}) if isinstance(products_includes, dict) else {}
    review_stats = prod_info.get("ReviewStatistics") or {}
    rating_dist_raw = review_stats.get("RatingDistribution") or []

    bv_dist = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
    for rd in rating_dist_raw:
        if isinstance(rd, dict):
            r_val = str(rd.get("RatingValue"))
            if r_val in bv_dist:
                bv_dist[r_val] = int(rd.get("Count") or 0)

    final_dist = {k: (bv_dist[k] if bv_dist[k] > 0 else local_dist[k]) for k in ("5", "4", "3", "2", "1")}

    bv_avg = review_stats.get("AverageOverallRating")
    avg_rating = round(sum(all_ratings) / len(all_ratings), 2) if all_ratings else (
        round(float(bv_avg), 2) if bv_avg is not None else None
    )

    bv_total = int(review_stats.get("TotalReviewCount") or 0)
    total_ratings = max(bv_total, sum(final_dist.values()), len(items))
    total_written = sum(1 for it in items if (it.get("body") or "").strip())

    # Recommended percentage
    rec_cnt = review_stats.get("RecommendedCount")
    not_rec_cnt = review_stats.get("NotRecommendedCount")
    rec_pct: Optional[float] = None
    if isinstance(rec_cnt, (int, float)) and isinstance(not_rec_cnt, (int, float)) and (rec_cnt + not_rec_cnt) > 0:
        rec_pct = round((rec_cnt / (rec_cnt + not_rec_cnt)) * 100.0, 1)
    else:
        rec_flags = [it["is_recommended"] for it in items if isinstance(it.get("is_recommended"), bool)]
        if rec_flags:
            rec_pct = round((sum(1 for f in rec_flags if f) / len(rec_flags)) * 100.0, 1)

    # Secondary / aspect ratings
    aspect_ratings: Dict[str, float] = {}
    sec_avgs = review_stats.get("SecondaryRatingsAverages") or {}
    if isinstance(sec_avgs, dict):
        for k, v in sec_avgs.items():
            if isinstance(v, dict) and v.get("AverageRating") is not None:
                label = v.get("Label") or str(k)
                try:
                    aspect_ratings[label] = round(float(v["AverageRating"]), 2)
                except (TypeError, ValueError):
                    pass

    if total_ratings == 0 and avg_rating is None and not items:
        return None, earliest_review_date

    review_v2 = build_review_v2(
        platform=platform,
        platform_product_id=product_id,
        parent_product_id=parent_product_id,
        seller_id=seller_id,
        average_rating=avg_rating,
        total_ratings=total_ratings,
        total_written_reviews=total_written,
        recommended_percentage=rec_pct,
        star_distribution=final_dist,
        aspect_ratings=aspect_ratings,
        product_review_items=items,
    )
    return review_v2, earliest_review_date


def fetch_laptop_reviews(
    product_id: str,
    session: requests.Session = None,
    limit: int = 100,
    days_cutoff: Optional[int] = 365,
    seller_id: Optional[str] = None,
    parent_product_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetches aggregate ratings and verified user reviews from Samsung's Bazaarvoice API.
    Filters reviews to those within `days_cutoff` days (or all-time when None).

    Returns:
        (schema_v2_review_doc, earliest_review_date)
    """
    close_session = False
    if session is None:
        session = requests.Session()
        close_session = True

    params = {
        "apiversion": "5.4",
        "passkey": BV_PASSKEY,
        "Filter": f"ProductId:{product_id}",
        "Include": "Products",
        "Stats": "Reviews",
        "Limit": str(limit),
        "Sort": "SubmissionTime:desc",
    }

    review_v2: Optional[Dict[str, Any]] = None
    earliest_review_date: Optional[str] = None

    try:
        url = f"{BV_API_URL}/reviews.json"
        resp = session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            review_v2, earliest_review_date = parse_bazaarvoice_response(
                data=data,
                product_id=product_id,
                platform="samsung",
                days_cutoff=days_cutoff,
                seller_id=seller_id,
                parent_product_id=parent_product_id,
            )
    except Exception as e:
        logger.warning(f"Failed to fetch Bazaarvoice reviews for {product_id}: {e}")
    finally:
        if close_session:
            session.close()

    return review_v2, earliest_review_date


fetch_product_reviews = fetch_laptop_reviews
