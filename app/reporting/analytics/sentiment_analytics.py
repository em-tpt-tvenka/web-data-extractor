"""Platform- and product-level sentiment segments."""

from __future__ import annotations

from collections import defaultdict

from app.reporting.schemas.report_metrics import PlatformMetric, ProductMetric
from app.reporting.utils.report_helpers import group_sentiment, round2


def platform_summary(reviews: list[dict]) -> tuple[list[PlatformMetric], dict]:
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for r in reviews:
        if r.get("platform"):
            by_platform[r["platform"]].append(r)

    metrics: list[PlatformMetric] = []
    for platform, revs in by_platform.items():
        gs = group_sentiment(revs)
        metrics.append(PlatformMetric(
            platform=platform, reviews=len(revs),
            net_sentiment=gs["net_sentiment"], positive_pct=gs["positive_pct"],
            negative_pct=gs["negative_pct"], average_rating=round2(gs["average_rating"]),
        ))
    metrics.sort(key=lambda m: m.reviews, reverse=True)
    index = {}
    for m in metrics:
        index[f"platform.{m.platform}.reviews"] = m.reviews
        index[f"platform.{m.platform}.net_sentiment"] = m.net_sentiment
    return metrics, index


def product_summary(reviews: list[dict], limit: int = 25) -> tuple[list[ProductMetric], dict]:
    by_product: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in reviews:
        key = (r.get("platform") or "", r.get("product_id") or "")
        by_product[key].append(r)

    metrics: list[ProductMetric] = []
    for (platform, product_id), revs in by_product.items():
        gs = group_sentiment(revs)
        first = revs[0]
        metrics.append(ProductMetric(
            product_id=product_id, platform=platform,
            product_name=first.get("product_name"), brand=first.get("brand"),
            reviews=len(revs), net_sentiment=gs["net_sentiment"],
            average_rating=round2(gs["average_rating"]),
        ))
    metrics.sort(key=lambda m: m.reviews, reverse=True)
    metrics = metrics[:limit]
    index = {"product.count": len(by_product)}
    return metrics, index
