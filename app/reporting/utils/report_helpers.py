"""Small pure helpers shared by analytics and generators."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def round2(value: float | int | None) -> float | None:
    return None if value is None else round(float(value), 2)


def safe_pct(numerator: float | int, denominator: float | int) -> float:
    """Percentage, rounded to 2 dp; 0.0 when the denominator is 0."""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100.0, 2)


def parse_review_date(value: Any) -> date | None:
    """Best-effort parse of the heterogeneous ``review_date`` field.

    Accepts ISO datetimes/dates (with or without a trailing ``Z``) and ``YYYY-MM``.
    Returns ``None`` for relative text ("2 months ago") or anything unparseable;
    callers treat undated reviews explicitly (kept in coverage, excluded from
    trends) rather than guessing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    # Try date-only / year-month prefixes.
    for fmt in ("%Y-%m-%d", "%Y-%m", "%d %b %Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def quarter_key(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year:04d}-Q{q}"


def mean(values: Iterable[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def net_sentiment(positive: int, negative: int, classified: int) -> float:
    """pos% - neg% over the classified denominator (see sentiment_classifier)."""
    if not classified:
        return 0.0
    return round((positive - negative) / classified * 100.0, 2)


def group_sentiment(reviews: list[dict]) -> dict[str, Any]:
    """Aggregate sentiment over a subset of enriched reviews.

    Returns counts, percentages over the classified denominator, net sentiment
    and average rating. Shared by brand/platform/product/topic analytics so the
    denominator rule is applied identically everywhere.
    """
    counts = {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0, "unclassified": 0}
    ratings: list[float] = []
    for r in reviews:
        counts[r.get("sentiment", "unclassified")] = counts.get(
            r.get("sentiment", "unclassified"), 0) + 1
        if r.get("rating") is not None:
            ratings.append(r["rating"])
    classified = counts["positive"] + counts["negative"] + counts["neutral"] + counts["mixed"]
    pos_pct = safe_pct(counts["positive"], classified)
    neg_pct = safe_pct(counts["negative"], classified)
    return {
        **counts,
        "classified": classified,
        "positive_pct": pos_pct,
        "negative_pct": neg_pct,
        "neutral_pct": safe_pct(counts["neutral"], classified),
        "mixed_pct": safe_pct(counts["mixed"], classified),
        "net_sentiment": round(pos_pct - neg_pct, 2),
        "average_rating": mean(ratings),
        "count": len(reviews),
    }
