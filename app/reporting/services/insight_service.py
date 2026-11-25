"""Generate, validate and persist section insights.

For each requested section the service tries the configured LLM provider; on any
failure, or when no provider is configured, it uses the deterministic narrator.
Either way the result is validated for schema + evidence traceability before an
``Insight`` is stored, and each insight records whether it came from the LLM or
the fallback.
"""

from __future__ import annotations

from app.core import identifiers
from app.core.config import settings
from app.core.logging import get_logger
from app.reporting import PROMPT_VERSION
from app.reporting.constants import SECTION_RECOMMENDATIONS
from app.reporting.llm import fallback_provider
from app.reporting.llm.base import LLMError
from app.reporting.llm.factory import get_provider
from app.reporting.llm.prompt_manager import SECTION_SPECS, build_context, render
from app.reporting.llm.response_validator import ValidatedSection, validate_section
from app.reporting.repositories.insight_repository import InsightRepository
from app.reporting.schemas.report_insight import Insight
from app.reporting.schemas.report_summary import ReportSummary
from app.reporting.utils.report_helpers import now_iso

_log = get_logger("reporting.insights")


class InsightGenerationResult:
    def __init__(self) -> None:
        self.insights: list[Insight] = []
        self.provider_label: str = "fallback"
        self.sections_via_llm: list[str] = []
        self.sections_via_fallback: list[str] = []


def _provider_model(name: str | None) -> str | None:
    name = (name or settings.llm_provider or "fallback").lower()
    return {
        "ollama": settings.ollama_model,
        "gemini": settings.gemini_model,
        "openrouter": settings.openrouter_model,
    }.get(name)


class InsightService:
    def __init__(self) -> None:
        self.repo = InsightRepository()

    async def generate(
        self,
        report_id: str,
        summary: ReportSummary,
        sections: list[str],
        *,
        llm_provider: str | None = None,
        persist: bool = True,
    ) -> InsightGenerationResult:
        result = InsightGenerationResult()
        provider = get_provider(llm_provider)
        model = _provider_model(llm_provider) if provider else None
        result.provider_label = f"{provider.name}:{model}" if provider else "fallback"

        for section in sections:
            if section not in SECTION_SPECS:
                continue  # rendered from metrics only (e.g. scorecard, topic table)
            validated, via_llm = await self._run_section(section, summary, provider, model)
            (result.sections_via_llm if via_llm else result.sections_via_fallback).append(section)
            result.insights.extend(
                self._to_insights(section, validated, report_id, summary,
                                   source="llm" if via_llm else "deterministic_fallback",
                                   model=model if via_llm else None)
            )

        if persist and result.insights:
            await self.repo.save_many(result.insights)
        _log.info("event=insights_generated report_id=%s total=%d via_llm=%d",
                  report_id, len(result.insights), len(result.sections_via_llm))
        return result

    async def _run_section(self, section, summary, provider, model) -> tuple[ValidatedSection, bool]:
        _, model_cls = SECTION_SPECS[section]
        schema = model_cls.model_json_schema()
        context = build_context(section, summary)

        if provider is not None:
            try:
                prompt = render(section, context, schema)
                raw = await provider.generate_structured(prompt, schema)
                validated = validate_section(section, raw, summary)
                if validated.ok:
                    return validated, True
                _log.warning("event=llm_output_rejected section=%s reason=%s",
                             section, validated.reason)
            except LLMError as exc:
                _log.warning("event=llm_failed section=%s error=%s", section, exc)

        # Deterministic fallback (always schema-valid and metric-backed).
        raw = fallback_provider.build(section, summary)
        return validate_section(section, raw, summary), False

    def _to_insights(self, section, validated: ValidatedSection, report_id,
                     summary: ReportSummary, *, source, model) -> list[Insight]:
        now = now_iso()
        out: list[Insight] = []
        for item in validated.items:
            if section == SECTION_RECOMMENDATIONS:
                out.append(Insight(
                    _id=identifiers.insight_id(),
                    tenant_id=summary.tenant_id, project_id=summary.project_id,
                    report_id=report_id, summary_id=summary.id, section=section,
                    title=item.get("area", "Recommendation"),
                    observation=item.get("recommendation", ""),
                    recommendation=item.get("recommendation"),
                    priority=item.get("priority"), impact=item.get("impact"),
                    effort=item.get("effort"),
                    supporting_metric_ids=item.get("supporting_metric_ids", []),
                    supporting_evidence_ids=item.get("supporting_evidence_ids", []),
                    validation_status=item.get("validation_status", "validated"),
                    validation_notes=item.get("validation_notes", []),
                    success_kpi=item.get("success_kpi"),
                    source=source, model=model, prompt_version=PROMPT_VERSION,
                    created_at=now,
                ))
            else:
                out.append(Insight(
                    _id=identifiers.insight_id(),
                    tenant_id=summary.tenant_id, project_id=summary.project_id,
                    report_id=report_id, summary_id=summary.id, section=section,
                    title=item.get("title", ""),
                    observation=item.get("observation", ""),
                    business_implication=item.get("business_implication"),
                    supporting_metric_ids=item.get("supporting_metric_ids", []),
                    supporting_evidence_ids=item.get("supporting_evidence_ids", []),
                    validation_status=item.get("validation_status", "validated"),
                    validation_notes=item.get("validation_notes", []),
                    source=source, model=model, prompt_version=PROMPT_VERSION,
                    created_at=now,
                ))
        return out
