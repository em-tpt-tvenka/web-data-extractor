"""Build section contexts and render prompts from templates.

Only aggregated metrics and a bounded set of evidence snippets are sent to the
LLM — never the full review corpus. Review text is wrapped and explicitly marked
as untrusted data so prompt-injection attempts inside a review cannot steer the
model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.reporting.constants import (
    SECTION_BRAND_COMPARISON,
    SECTION_CATEGORY,
    SECTION_COMPLAINT,
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_RECOMMENDATIONS,
    SECTION_RISK,
    SECTION_SENTIMENT,
)
from app.reporting.schemas.report_insight import RecommendationsOut, SectionInsightOut
from app.reporting.schemas.report_summary import ReportSummary

_PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"

# section -> (template filename, response model)
SECTION_SPECS: dict[str, tuple[str, Any]] = {
    SECTION_EXECUTIVE_SUMMARY: ("executive_summary.txt", SectionInsightOut),
    SECTION_BRAND_COMPARISON: ("brand_comparison.txt", SectionInsightOut),
    SECTION_SENTIMENT: ("sentiment_analysis.txt", SectionInsightOut),
    SECTION_CATEGORY: ("category_analysis.txt", SectionInsightOut),
    SECTION_COMPLAINT: ("complaint_analysis.txt", SectionInsightOut),
    SECTION_RISK: ("risk_analysis.txt", SectionInsightOut),
    SECTION_RECOMMENDATIONS: ("recommendation_generation.txt", RecommendationsOut),
}


def _evidence_for(summary: ReportSummary, section: str, limit: int = 6) -> list[dict]:
    refs = [e for e in summary.evidence if e.section == section]
    if len(refs) < limit:
        refs += [e for e in summary.evidence if e.section != section]
    out = []
    for e in refs[:limit]:
        out.append({"evidence_id": e.review_id, "brand": e.brand,
                    "rating": e.rating, "sentiment": e.sentiment, "text": e.snippet})
    return out


def build_context(section: str, summary: ReportSummary) -> dict[str, Any]:
    """Tailored, compact metric context for one section."""
    s = summary
    base: dict[str, Any] = {
        "report_filters": s.filters.model_dump(),
        "data_coverage": s.data_coverage.model_dump(),
        "evidence": _evidence_for(summary, section),
    }
    if section == SECTION_EXECUTIVE_SUMMARY:
        base.update({
            "sentiment": s.sentiment_summary.model_dump(),
            "top_brands": [b.model_dump() for b in s.brand_metrics[:6]],
            "top_complaints": [c.model_dump() for c in s.complaint_summary[:6]],
            "top_risks": [r.model_dump() for r in s.risk_summary[:5]],
            "trend": s.monthly_trends.model_dump(),
            "competitive_insights": [c.model_dump() for c in s.competitive_insights],
        })
    elif section == SECTION_BRAND_COMPARISON:
        base.update({
            "brands": [b.model_dump() for b in s.brand_metrics],
            "competitive_insights": [c.model_dump() for c in s.competitive_insights],
        })
    elif section == SECTION_SENTIMENT:
        base.update({
            "sentiment": s.sentiment_summary.model_dump(),
            "platforms": [p.model_dump() for p in s.platform_summary],
            "trend": s.monthly_trends.model_dump(),
        })
    elif section == SECTION_CATEGORY:
        base.update({
            "topics": [t.model_dump() for t in s.topic_summary[:12]],
            "top_products": [p.model_dump() for p in s.product_summary[:10]],
        })
    elif section == SECTION_COMPLAINT:
        base.update({
            "complaints": [c.model_dump() for c in s.complaint_summary],
            "sentiment": s.sentiment_summary.model_dump(),
        })
    elif section == SECTION_RISK:
        base.update({
            "risks": [r.model_dump() for r in s.risk_summary[:10]],
            "risk_rule": "escalate = volume>=min AND negative_share>=threshold AND QoQ_growth>=threshold",
        })
    elif section == SECTION_RECOMMENDATIONS:
        base.update({
            "sentiment": s.sentiment_summary.model_dump(),
            "top_complaints": [c.model_dump() for c in s.complaint_summary[:6]],
            "top_risks": [r.model_dump() for r in s.risk_summary[:5]],
            "brand_gaps": [b.model_dump() for b in s.brand_metrics[:6]],
            "topics": [t.model_dump() for t in s.topic_summary[:8]],
        })

    # The exact metric ids this section may cite.
    base["available_metric_ids"] = _section_metric_ids(section, summary)
    return base


def _section_metric_ids(section: str, summary: ReportSummary) -> list[str]:
    keys = list(summary.metric_index.keys())
    prefixes = {
        SECTION_EXECUTIVE_SUMMARY: ("sentiment.", "brand.", "complaint.", "risk.", "trend.", "coverage."),
        SECTION_BRAND_COMPARISON: ("brand.", "sentiment."),
        SECTION_SENTIMENT: ("sentiment.", "platform.", "trend."),
        SECTION_CATEGORY: ("topic.", "product.", "coverage."),
        SECTION_COMPLAINT: ("complaint.", "sentiment."),
        SECTION_RISK: ("risk.",),
        SECTION_RECOMMENDATIONS: ("sentiment.", "complaint.", "risk.", "brand.", "topic."),
    }.get(section, tuple())
    return [k for k in keys if k.startswith(prefixes)] if prefixes else keys


def render(section: str, context: dict[str, Any], response_schema: dict) -> str:
    template_file, _ = SECTION_SPECS[section]
    template = (_PROMPT_DIR / template_file).read_text(encoding="utf-8")
    return template.format(
        context_json=json.dumps(context, indent=2, default=str),
        schema_json=json.dumps(response_schema, indent=2, default=str),
    )
