"""General coverage/quality KPIs and overall sentiment."""

from __future__ import annotations

from app.reporting.constants import Sentiment
from app.reporting.schemas.report_metrics import (
    DataCoverage,
    DataQuality,
    SentimentSummary,
)
from app.reporting.utils.report_helpers import mean, round2, safe_pct


def data_coverage(reviews: list[dict], product_count: int) -> DataCoverage:
    brands = {r.get("brand") for r in reviews if r.get("brand")}
    cats = {r.get("category") for r in reviews if r.get("category")}
    platforms = {r.get("platform") for r in reviews if r.get("platform")}
    dates = sorted(r["date"] for r in reviews if r.get("date"))
    with_rating = sum(1 for r in reviews if r.get("rating") is not None)
    with_text = sum(1 for r in reviews if (r.get("text") or "").strip())
    with_date = len(dates)
    # Reviews collection is already de-duplicated by (_id); duplicates are rare,
    # but we report the figure for transparency.
    unique_ids = {r.get("review_id") for r in reviews}
    return DataCoverage(
        total_products=product_count,
        total_reviews=len(reviews),
        unique_reviews=len(unique_ids),
        duplicate_reviews=max(0, len(reviews) - len(unique_ids)),
        platforms=len(platforms),
        brands=len(brands),
        categories=len(cats),
        reviews_with_rating=with_rating,
        reviews_with_text=with_text,
        reviews_with_date=with_date,
        date_min=dates[0].isoformat() if dates else None,
        date_max=dates[-1].isoformat() if dates else None,
    )


def data_quality(reviews: list[dict]) -> DataQuality:
    total = len(reviews) or 1
    classified = sum(
        1 for r in reviews if r.get("sentiment") != Sentiment.unclassified.value
    )
    notes = [
        "Sentiment is derived from the 1-5 star rating, refined by a polarity "
        "lexicon; reviews with neither a rating nor lexical signal are "
        "'unclassified' and excluded from sentiment percentages.",
    ]
    return DataQuality(
        classified_reviews=classified,
        unclassified_reviews=len(reviews) - classified,
        missing_rating_pct=safe_pct(sum(1 for r in reviews if r.get("rating") is None), total),
        missing_text_pct=safe_pct(sum(1 for r in reviews if not (r.get("text") or "").strip()), total),
        missing_date_pct=safe_pct(sum(1 for r in reviews if not r.get("date")), total),
        missing_brand_pct=safe_pct(sum(1 for r in reviews if not r.get("brand")), total),
        missing_category_pct=safe_pct(sum(1 for r in reviews if not r.get("category")), total),
        notes=notes,
    )


def sentiment_summary(reviews: list[dict]) -> SentimentSummary:
    counts = {s.value: 0 for s in Sentiment}
    for r in reviews:
        counts[r.get("sentiment", Sentiment.unclassified.value)] += 1
    pos, neg = counts["positive"], counts["negative"]
    neu, mixed = counts["neutral"], counts["mixed"]
    classified = pos + neg + neu + mixed
    ratings = [r["rating"] for r in reviews if r.get("rating") is not None]
    return SentimentSummary(
        positive=pos, negative=neg, neutral=neu, mixed=mixed,
        unclassified=counts["unclassified"], classified=classified,
        positive_pct=safe_pct(pos, classified),
        negative_pct=safe_pct(neg, classified),
        neutral_pct=safe_pct(neu, classified),
        mixed_pct=safe_pct(mixed, classified),
        net_sentiment=round((safe_pct(pos, classified) - safe_pct(neg, classified)), 2),
        average_rating=round2(mean(ratings)),
    )


def metric_index(coverage: DataCoverage, sentiment: SentimentSummary) -> dict:
    return {
        "coverage.total_reviews": coverage.total_reviews,
        "coverage.total_products": coverage.total_products,
        "coverage.brands": coverage.brands,
        "coverage.platforms": coverage.platforms,
        "sentiment.positive_pct": sentiment.positive_pct,
        "sentiment.negative_pct": sentiment.negative_pct,
        "sentiment.neutral_pct": sentiment.neutral_pct,
        "sentiment.net": sentiment.net_sentiment,
        "sentiment.average_rating": sentiment.average_rating,
    }
