"""Cross-brand competitive insights derived from brand metrics."""

from __future__ import annotations

from app.reporting.schemas.report_metrics import BrandMetric, CompetitiveInsight


def competitive_insights(brands: list[BrandMetric]) -> list[CompetitiveInsight]:
    if len(brands) < 2:
        return []
    out: list[CompetitiveInsight] = []

    sov_leader = max(brands, key=lambda b: b.share_of_voice_pct)
    out.append(CompetitiveInsight(
        dimension="share_of_voice",
        leader=sov_leader.brand,
        detail=f"{sov_leader.brand} leads share of voice at {sov_leader.share_of_voice_pct}%.",
    ))

    sent_leader = max(brands, key=lambda b: b.net_sentiment)
    sent_lagg = min(brands, key=lambda b: b.net_sentiment)
    out.append(CompetitiveInsight(
        dimension="net_sentiment",
        leader=sent_leader.brand,
        laggard=sent_lagg.brand,
        detail=(f"{sent_leader.brand} has the strongest net sentiment "
                f"({sent_leader.net_sentiment}); {sent_lagg.brand} the weakest "
                f"({sent_lagg.net_sentiment})."),
    ))

    # SoV leader vs sentiment leader divergence (the report's "attention vs
    # conversion" angle).
    if sov_leader.brand != sent_leader.brand:
        out.append(CompetitiveInsight(
            dimension="attention_vs_conversion",
            leader=sent_leader.brand,
            laggard=sov_leader.brand,
            detail=(f"{sov_leader.brand} is talked about most but {sent_leader.brand} "
                    f"converts attention into better sentiment."),
        ))
    return out
