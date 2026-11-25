"""Complaint-category aggregation (over negative/mixed reviews only)."""

from __future__ import annotations

from collections import Counter

from app.reporting.schemas.report_metrics import ComplaintMetric
from app.reporting.utils.report_helpers import safe_pct


def complaint_summary(reviews: list[dict]) -> tuple[list[ComplaintMetric], dict]:
    counter: Counter[str] = Counter()
    for r in reviews:
        for c in r.get("complaints", []):
            counter[c] += 1
    total = sum(counter.values())

    metrics = [
        ComplaintMetric(
            category=cat, count=n, share_of_complaints_pct=safe_pct(n, total)
        )
        for cat, n in counter.most_common()
    ]
    index = {"complaint.total": total}
    for m in metrics:
        index[f"complaint.{m.category}.count"] = m.count
        index[f"complaint.{m.category}.share_pct"] = m.share_of_complaints_pct
    return metrics, index
