"""Time-series trends with guarded growth calculations."""

from __future__ import annotations

from collections import defaultdict

from app.reporting.schemas.report_metrics import TrendPoint, TrendSummary
from app.reporting.utils.report_helpers import (
    group_sentiment,
    month_key,
    quarter_key,
    round2,
    safe_pct,
)


def _buckets(reviews: list[dict], keyfn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        if r.get("date"):
            out[keyfn(r["date"])].append(r)
    return out


def _growth(curr: int, prev: int) -> tuple[float | None, str | None]:
    """Guarded period-over-period growth. Returns ``(pct, note)`` where ``pct``
    is ``None`` when it cannot be computed honestly."""
    if prev == 0:
        return None, "no comparable prior period (prior volume was zero)"
    return round((curr - prev) / prev * 100.0, 2), None


def monthly_trends(reviews: list[dict]) -> tuple[TrendSummary, dict]:
    dated = [r for r in reviews if r.get("date")]
    note = None
    if not dated:
        return (
            TrendSummary(granularity="monthly", points=[],
                         note="No dated reviews; trend analysis unavailable."),
            {"trend.points": 0},
        )

    months = _buckets(reviews, month_key)
    points: list[TrendPoint] = []
    for key in sorted(months):
        gs = group_sentiment(months[key])
        points.append(TrendPoint(
            period=key, reviews=len(months[key]),
            net_sentiment=gs["net_sentiment"], average_rating=round2(gs["average_rating"]),
        ))

    mom, mom_note = (None, None)
    if len(points) >= 2:
        mom, mom_note = _growth(points[-1].reviews, points[-2].reviews)

    quarters = _buckets(reviews, quarter_key)
    qkeys = sorted(quarters)
    qoq, qoq_note = (None, None)
    if len(qkeys) >= 2:
        qoq, qoq_note = _growth(len(quarters[qkeys[-1]]), len(quarters[qkeys[-2]]))

    note = next((n for n in (mom_note, qoq_note) if n), None)
    summary = TrendSummary(
        granularity="monthly", points=points,
        mom_growth_pct=mom, qoq_growth_pct=qoq, note=note,
    )
    index = {
        "trend.points": len(points),
        "trend.mom_growth_pct": mom,
        "trend.qoq_growth_pct": qoq,
        "trend.latest_month_reviews": points[-1].reviews if points else 0,
    }
    return summary, index
