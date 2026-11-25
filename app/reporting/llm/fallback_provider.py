"""Deterministic narrator used when no LLM is configured or a call fails.

It composes section narrative directly from the computed metrics, so a report is
always produced, every statement is metric-backed, and nothing is fabricated.
Output matches the same per-section response schema the LLM must return, and is
labelled ``source="deterministic_fallback"`` by the insight service.
"""

from __future__ import annotations

from app.reporting.constants import (
    SECTION_BRAND_COMPARISON,
    SECTION_CATEGORY,
    SECTION_COMPLAINT,
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_RECOMMENDATIONS,
    SECTION_RISK,
    SECTION_SENTIMENT,
)
from app.reporting.schemas.report_summary import ReportSummary


def _evidence_ids(summary: ReportSummary, section: str, n: int = 2) -> list[str]:
    ids = [e.review_id for e in summary.evidence if e.section == section]
    return ids[:n]


def _point(title, observation, implication, metric_ids, evidence_ids=None) -> dict:
    return {
        "title": title,
        "observation": observation,
        "business_implication": implication,
        "supporting_metric_ids": metric_ids,
        "supporting_evidence_ids": evidence_ids or [],
    }


def build(section: str, summary: ReportSummary) -> dict:
    if section == SECTION_EXECUTIVE_SUMMARY:
        return _executive(summary)
    if section == SECTION_BRAND_COMPARISON:
        return _brand(summary)
    if section == SECTION_SENTIMENT:
        return _sentiment(summary)
    if section == SECTION_CATEGORY:
        return _category(summary)
    if section == SECTION_COMPLAINT:
        return _complaint(summary)
    if section == SECTION_RISK:
        return _risk(summary)
    if section == SECTION_RECOMMENDATIONS:
        return _recommendations(summary)
    return {"headline": "", "points": []}


def _executive(s: ReportSummary) -> dict:
    cov, sent = s.data_coverage, s.sentiment_summary
    points = [
        _point(
            "Overall customer sentiment",
            f"Across {cov.total_reviews} reviews on {cov.platforms} platform(s), "
            f"net sentiment is {sent.net_sentiment} ({sent.positive_pct}% positive, "
            f"{sent.negative_pct}% negative).",
            "Sets the baseline for every action below.",
            ["sentiment.net", "sentiment.positive_pct", "sentiment.negative_pct",
             "coverage.total_reviews"],
            _evidence_ids(s, SECTION_SENTIMENT),
        )
    ]
    if s.brand_metrics:
        top = max(s.brand_metrics, key=lambda b: b.net_sentiment)
        points.append(_point(
            "Sentiment leader",
            f"{top.brand} has the strongest net sentiment ({top.net_sentiment}) "
            f"on {top.mentions} mentions ({top.share_of_voice_pct}% share of voice).",
            "A benchmark the weaker brands trail.",
            [f"brand.{top.brand}.net_sentiment", f"brand.{top.brand}.share_of_voice_pct"],
            _evidence_ids(s, SECTION_BRAND_COMPARISON),
        ))
    if s.complaint_summary:
        c = s.complaint_summary[0]
        points.append(_point(
            "Top complaint driver",
            f"'{c.category}' is the leading complaint category "
            f"({c.count} complaints, {c.share_of_complaints_pct}% of all complaints).",
            "The largest single lever on negative sentiment.",
            [f"complaint.{c.category}.count", f"complaint.{c.category}.share_pct"],
            _evidence_ids(s, SECTION_COMPLAINT),
        ))
    escalate = [r for r in s.risk_summary if r.zone == "escalate"]
    if escalate:
        r = escalate[0]
        points.append(_point(
            "Risk signal",
            f"'{r.theme}' is in the escalate zone ({r.negative_share_pct}% negative, "
            f"QoQ growth {r.growth_qoq_pct}%).",
            "Needs a pre-emptive response before it grows.",
            [f"risk.{r.theme}.zone", f"risk.{r.theme}.negative_share_pct"],
        ))
    if s.monthly_trends.qoq_growth_pct is not None:
        points.append(_point(
            "Volume trend",
            f"Review volume moved {s.monthly_trends.qoq_growth_pct}% quarter-on-quarter.",
            "Indicates whether attention is rising or cooling.",
            ["trend.qoq_growth_pct"],
        ))
    return {"headline":
            f"Net sentiment {s.sentiment_summary.net_sentiment} across "
            f"{s.data_coverage.total_reviews} reviews.", "points": points}


def _brand(s: ReportSummary) -> dict:
    if not s.brand_metrics:
        return {"headline": "No brand-attributed reviews in scope.", "points": []}
    points = []
    sov = max(s.brand_metrics, key=lambda b: b.share_of_voice_pct)
    points.append(_point(
        "Share of voice",
        f"{sov.brand} leads share of voice at {sov.share_of_voice_pct}% "
        f"({sov.mentions} mentions).",
        "Most-discussed brand in scope.",
        [f"brand.{sov.brand}.share_of_voice_pct", f"brand.{sov.brand}.mentions"],
    ))
    lead = max(s.brand_metrics, key=lambda b: b.net_sentiment)
    lag = min(s.brand_metrics, key=lambda b: b.net_sentiment)
    points.append(_point(
        "Sentiment spread",
        f"{lead.brand} tops net sentiment ({lead.net_sentiment}); "
        f"{lag.brand} is lowest ({lag.net_sentiment}).",
        "Shows where advocacy and risk concentrate.",
        [f"brand.{lead.brand}.net_sentiment", f"brand.{lag.brand}.net_sentiment"],
        _evidence_ids(s, SECTION_BRAND_COMPARISON),
    ))
    for b in s.brand_metrics[:3]:
        if b.top_themes:
            t = b.top_themes[0]
            points.append(_point(
                f"{b.brand}: dominant theme",
                f"{b.brand}'s most-discussed theme is '{t['theme']}' "
                f"({t['mentions']} mentions, net {t['net_sentiment']}).",
                "Defines what this brand is known for.",
                [f"brand.{b.brand}.mentions"],
            ))
    return {"headline": f"{sov.brand} leads attention; {lead.brand} leads sentiment.",
            "points": points}


def _sentiment(s: ReportSummary) -> dict:
    sent = s.sentiment_summary
    points = [_point(
        "Sentiment split",
        f"{sent.positive_pct}% positive, {sent.neutral_pct}% neutral, "
        f"{sent.negative_pct}% negative (net {sent.net_sentiment}); "
        f"average rating {sent.average_rating}.",
        "The headline health metric.",
        ["sentiment.positive_pct", "sentiment.negative_pct", "sentiment.net",
         "sentiment.average_rating"],
    )]
    if s.platform_summary:
        best = max(s.platform_summary, key=lambda p: p.net_sentiment)
        worst = min(s.platform_summary, key=lambda p: p.net_sentiment)
        points.append(_point(
            "By platform",
            f"{best.platform} is most positive (net {best.net_sentiment}); "
            f"{worst.platform} least (net {worst.net_sentiment}).",
            "Guides where to steer high-value traffic.",
            [f"platform.{best.platform}.net_sentiment",
             f"platform.{worst.platform}.net_sentiment"],
        ))
    if s.monthly_trends.points:
        points.append(_point(
            "Trend",
            f"{len(s.monthly_trends.points)} months of data; "
            f"MoM volume growth {s.monthly_trends.mom_growth_pct}.",
            "Separates a one-off spike from a shift.",
            ["trend.mom_growth_pct", "trend.points"],
        ))
    return {"headline": f"Net sentiment {sent.net_sentiment}.", "points": points}


def _category(s: ReportSummary) -> dict:
    if not s.topic_summary:
        return {"headline": "No themes detected in scope.", "points": []}
    points = []
    for t in s.topic_summary[:5]:
        points.append(_point(
            f"Theme: {t.theme}",
            f"'{t.theme}' ({t.category}) has {t.mentions} mentions, net "
            f"{t.net_sentiment}, dominant sentiment {t.dominant_sentiment}.",
            "Positive cue to amplify." if t.net_sentiment >= 0 else "Negative driver to fix.",
            [f"topic.{t.theme}.mentions", f"topic.{t.theme}.net_sentiment"],
        ))
    return {"headline": f"{s.topic_summary[0].theme} is the most-discussed theme.",
            "points": points}


def _complaint(s: ReportSummary) -> dict:
    if not s.complaint_summary:
        return {"headline": "No complaints detected in scope.", "points": []}
    points = []
    for c in s.complaint_summary[:6]:
        points.append(_point(
            f"Complaint: {c.category}",
            f"'{c.category}' accounts for {c.count} complaints "
            f"({c.share_of_complaints_pct}% of all complaints).",
            "A concrete service/product fix target.",
            [f"complaint.{c.category}.count", f"complaint.{c.category}.share_pct"],
            _evidence_ids(s, SECTION_COMPLAINT),
        ))
    return {"headline": f"'{s.complaint_summary[0].category}' is the top complaint.",
            "points": points}


def _risk(s: ReportSummary) -> dict:
    flagged = [r for r in s.risk_summary if r.zone in ("escalate", "monitor")]
    if not flagged:
        return {"headline": "No theme meets the escalate rule this period.",
                "points": [_point(
                    "No active risk",
                    "No theme met volume + negative-share + QoQ-growth thresholds.",
                    "Continue routine monitoring.", ["risk.escalate_count"])]}
    points = []
    for r in flagged[:6]:
        points.append(_point(
            f"{r.zone.title()}: {r.theme}",
            f"'{r.theme}' — {r.negative_share_pct}% negative, QoQ growth "
            f"{r.growth_qoq_pct}, volume {r.volume}. {r.rationale}",
            "Act before it escalates." if r.zone == "monitor" else "Immediate attention.",
            [f"risk.{r.theme}.zone", f"risk.{r.theme}.negative_share_pct"],
        ))
    return {"headline": f"{len(flagged)} theme(s) need watching.", "points": points}


def _recommendations(s: ReportSummary) -> dict:
    recs = []
    for c in s.complaint_summary[:3]:
        recs.append({
            "area": "Customer Experience",
            "recommendation": f"Reduce '{c.category}' complaints through a targeted fix.",
            "priority": "P1" if c.share_of_complaints_pct >= 20 else "P2",
            "impact": "High" if c.share_of_complaints_pct >= 20 else "Medium",
            "effort": "Medium",
            "supporting_metric_ids": [f"complaint.{c.category}.count",
                                      f"complaint.{c.category}.share_pct"],
            "success_kpi": f"Lower share of '{c.category}' complaints next period",
        })
    for r in [r for r in s.risk_summary if r.zone == "escalate"][:2]:
        recs.append({
            "area": "Product / Quality",
            "recommendation": f"Pre-empt the rising '{r.theme}' issue.",
            "priority": "P1", "impact": "High", "effort": "Medium",
            "supporting_metric_ids": [f"risk.{r.theme}.zone",
                                      f"risk.{r.theme}.negative_share_pct"],
            "success_kpi": f"'{r.theme}' negative share falls below threshold",
        })
    if s.brand_metrics:
        lag = min(s.brand_metrics, key=lambda b: b.net_sentiment)
        recs.append({
            "area": "Marketing",
            "recommendation": f"Address {lag.brand}'s sentiment gap vs peers.",
            "priority": "P2", "impact": "Medium", "effort": "Low",
            "supporting_metric_ids": [f"brand.{lag.brand}.net_sentiment"],
            "success_kpi": f"{lag.brand} net sentiment improves next period",
        })
    if not recs:
        recs.append({
            "area": "Measurement",
            "recommendation": "Expand review coverage to strengthen future analysis.",
            "priority": "P3", "impact": "Low", "effort": "Low",
            "supporting_metric_ids": ["coverage.total_reviews"],
            "success_kpi": "More reviews collected next period",
        })
    return {"recommendations": recs}
