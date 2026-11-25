"""Per-brand metrics: volume, share of voice, sentiment, top themes."""

from __future__ import annotations

from collections import Counter, defaultdict

from app.reporting.schemas.report_metrics import BrandMetric
from app.reporting.utils.report_helpers import group_sentiment, round2, safe_pct


def _top_themes(reviews: list[dict], limit: int = 5) -> list[dict]:
    theme_reviews: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        for a in r.get("aspects", []):
            theme_reviews[a["theme"]].append(r)
    ranked = sorted(theme_reviews.items(), key=lambda kv: len(kv[1]), reverse=True)[:limit]
    out = []
    for theme, revs in ranked:
        gs = group_sentiment(revs)
        out.append({"theme": theme, "mentions": len(revs), "net_sentiment": gs["net_sentiment"]})
    return out


def brand_metrics(reviews: list[dict]) -> tuple[list[BrandMetric], dict]:
    """Return brand metrics (sorted by mentions desc) and a metric_index fragment.

    Share of voice is computed over reviews that carry a brand; brandless reviews
    are excluded from the SoV denominator (and that exclusion is reported).
    """
    branded = [r for r in reviews if r.get("brand")]
    by_brand: dict[str, list[dict]] = defaultdict(list)
    for r in branded:
        by_brand[r["brand"]].append(r)

    total_branded = len(branded)
    metrics: list[BrandMetric] = []
    for brand, revs in by_brand.items():
        gs = group_sentiment(revs)
        metrics.append(BrandMetric(
            brand=brand,
            mentions=len(revs),
            share_of_voice_pct=safe_pct(len(revs), total_branded),
            net_sentiment=gs["net_sentiment"],
            positive_pct=gs["positive_pct"],
            negative_pct=gs["negative_pct"],
            average_rating=round2(gs["average_rating"]),
            top_themes=_top_themes(revs),
        ))
    metrics.sort(key=lambda m: m.mentions, reverse=True)

    index: dict = {"brand.count": len(metrics), "brand.branded_reviews": total_branded}
    for m in metrics:
        index[f"brand.{m.brand}.mentions"] = m.mentions
        index[f"brand.{m.brand}.share_of_voice_pct"] = m.share_of_voice_pct
        index[f"brand.{m.brand}.net_sentiment"] = m.net_sentiment
        index[f"brand.{m.brand}.positive_pct"] = m.positive_pct
        index[f"brand.{m.brand}.negative_pct"] = m.negative_pct
        index[f"brand.{m.brand}.average_rating"] = m.average_rating
    return metrics, index
