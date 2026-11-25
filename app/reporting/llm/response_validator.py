"""Validate a section response (from an LLM or the deterministic narrator) for
schema compliance and evidence traceability.

Guarantees enforced:
  * the response parses against the section's Pydantic schema;
  * every cited ``supporting_metric_id`` exists in the summary's metric_index
    (unknown ids are dropped and noted);
  * every cited ``supporting_evidence_id`` exists in the summary's evidence set,
    within the same tenant/project (cross-tenant refs are impossible because
    evidence is drawn only from this summary);
  * a point/recommendation with no valid metric id is kept but flagged
    ``validation_status="unsupported"`` so it is never presented as fact.

What it cannot do: verify free-text numbers against the metric values. Narrative
is therefore anchored to metric ids (traceable), and the numbers shown in the
PDF/Excel come from the computed metrics, not from the model's prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.core.logging import get_logger
from app.reporting.constants import SECTION_RECOMMENDATIONS
from app.reporting.llm.prompt_manager import SECTION_SPECS
from app.reporting.schemas.report_summary import ReportSummary

_log = get_logger("reporting.llm.validator")


@dataclass
class ValidatedSection:
    ok: bool
    section: str
    headline: str = ""
    items: list[dict] = field(default_factory=list)  # points or recommendations
    reason: str | None = None


def _clean_refs(
    metric_ids: list[str], evidence_ids: list[str],
    valid_metrics: set[str], valid_evidence: set[str],
) -> tuple[list[str], list[str], list[str]]:
    notes: list[str] = []
    kept_metrics = [m for m in metric_ids if m in valid_metrics]
    dropped_m = [m for m in metric_ids if m not in valid_metrics]
    if dropped_m:
        notes.append(f"dropped unknown metric ids: {dropped_m}")
    kept_evidence = [e for e in evidence_ids if e in valid_evidence]
    dropped_e = [e for e in evidence_ids if e not in valid_evidence]
    if dropped_e:
        notes.append(f"dropped unknown evidence ids: {dropped_e}")
    return kept_metrics, kept_evidence, notes


def validate_section(
    section: str, raw: dict[str, Any], summary: ReportSummary
) -> ValidatedSection:
    _, model_cls = SECTION_SPECS[section]
    try:
        parsed = model_cls.model_validate(raw)
    except ValidationError as exc:
        _log.warning("event=schema_invalid section=%s error=%s", section, exc.errors()[:2])
        return ValidatedSection(ok=False, section=section, reason=f"schema: {exc.errors()[:1]}")

    valid_metrics = set(summary.metric_index.keys())
    valid_evidence = {e.review_id for e in summary.evidence}

    items: list[dict] = []
    if section == SECTION_RECOMMENDATIONS:
        for rec in parsed.recommendations:
            d = rec.model_dump()
            metrics, _ev, notes = _clean_refs(
                d.get("supporting_metric_ids", []), [], valid_metrics, valid_evidence)
            d["supporting_metric_ids"] = metrics
            d["supporting_evidence_ids"] = []
            d["validation_notes"] = notes
            d["validation_status"] = "validated" if metrics else "unsupported"
            items.append(d)
        return ValidatedSection(ok=True, section=section, headline="", items=items)

    headline = getattr(parsed, "headline", "")
    for pt in parsed.points:
        d = pt.model_dump()
        metrics, evidence, notes = _clean_refs(
            d.get("supporting_metric_ids", []), d.get("supporting_evidence_ids", []),
            valid_metrics, valid_evidence)
        d["supporting_metric_ids"] = metrics
        d["supporting_evidence_ids"] = evidence
        d["validation_notes"] = notes
        d["validation_status"] = "validated" if metrics else "unsupported"
        items.append(d)
    return ValidatedSection(ok=True, section=section, headline=headline, items=items)
