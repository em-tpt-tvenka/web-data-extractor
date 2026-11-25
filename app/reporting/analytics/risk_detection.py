"""Configurable risk rules over themes (growing + negative + material volume)."""

from __future__ import annotations

from collections import defaultdict

from app.reporting.schemas.report_metrics import RiskItem
from app.reporting.utils.report_helpers import quarter_key, safe_pct


def risk_summary(
    reviews: list[dict],
    *,
    growth_threshold: float,
    negative_share_threshold: float,
    min_volume: int,
) -> tuple[list[RiskItem], dict]:
    """Flag themes that are (a) growing fast QoQ, (b) majority-negative and
    (c) above a material volume. Thresholds are injected (configurable) and the
    rule is echoed into each item's rationale. QoQ growth is only computed when
    two quarters of data exist; otherwise it stays ``None`` and cannot escalate.
    """
    # Determine the two most recent quarters present.
    quarters = sorted({quarter_key(r["date"]) for r in reviews if r.get("date")})
    curr_q = quarters[-1] if quarters else None
    prev_q = quarters[-2] if len(quarters) >= 2 else None

    themes: dict[str, dict] = defaultdict(
        lambda: {"category": "", "reviews": [], "curr": 0, "prev": 0}
    )
    for r in reviews:
        q = quarter_key(r["date"]) if r.get("date") else None
        seen: set[str] = set()
        for a in r.get("aspects", []):
            theme = a["theme"]
            if theme in seen:
                continue
            seen.add(theme)
            t = themes[theme]
            t["category"] = a["category"]
            t["reviews"].append(r)
            if q and q == curr_q:
                t["curr"] += 1
            elif q and q == prev_q:
                t["prev"] += 1

    items: list[RiskItem] = []
    for theme, t in themes.items():
        revs = t["reviews"]
        volume = len(revs)
        neg = sum(1 for r in revs if r.get("sentiment") in ("negative", "mixed"))
        classified = sum(
            1 for r in revs if r.get("sentiment") != "unclassified"
        )
        neg_share = safe_pct(neg, classified)

        growth = None
        if prev_q is not None and t["prev"] > 0:
            growth = round((t["curr"] - t["prev"]) / t["prev"] * 100.0, 2)

        escalate = (
            volume >= min_volume
            and neg_share >= negative_share_threshold
            and growth is not None
            and growth >= growth_threshold
        )
        if escalate:
            zone = "escalate"
        elif neg_share >= negative_share_threshold and volume >= min_volume:
            zone = "monitor"
        else:
            zone = "watch"

        rationale = (
            f"volume={volume} (min {min_volume}), negative_share={neg_share}% "
            f"(threshold {negative_share_threshold}%), "
            f"QoQ_growth={growth if growth is not None else 'n/a'}% "
            f"(threshold {growth_threshold}%)."
        )
        items.append(RiskItem(
            theme=theme, category=t["category"], volume=volume,
            negative_share_pct=neg_share, growth_qoq_pct=growth,
            zone=zone, rule_triggered=escalate, rationale=rationale,
        ))

    # Most concerning first.
    zone_rank = {"escalate": 0, "monitor": 1, "watch": 2}
    items.sort(key=lambda i: (zone_rank[i.zone], -i.negative_share_pct, -i.volume))

    index = {
        "risk.escalate_count": sum(1 for i in items if i.zone == "escalate"),
        "risk.monitor_count": sum(1 for i in items if i.zone == "monitor"),
        "risk.has_prior_quarter": prev_q is not None,
    }
    for i in items:
        index[f"risk.{i.theme}.zone"] = i.zone
        index[f"risk.{i.theme}.negative_share_pct"] = i.negative_share_pct
        if i.growth_qoq_pct is not None:
            index[f"risk.{i.theme}.growth_qoq_pct"] = i.growth_qoq_pct
    return items, index
