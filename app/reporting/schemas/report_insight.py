"""LLM-generated (or deterministically generated) insights and recommendations,
plus the structured response shapes each section prompt must return."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="allow")
    area: str
    recommendation: str
    priority: str = "P2"          # P1 | P2 | P3
    impact: str = "Medium"        # High | Medium | Low
    effort: str = "Medium"        # High | Medium | Low
    supporting_metric_ids: list[str] = Field(default_factory=list)
    success_kpi: str | None = None


class Insight(BaseModel):
    """One stored insight (collection ``report_insights``)."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="_id")
    tenant_id: str | None = None
    project_id: str | None = None
    report_id: str
    summary_id: str
    section: str
    title: str
    observation: str
    business_implication: str | None = None
    recommendation: str | None = None
    priority: str | None = None
    impact: str | None = None
    effort: str | None = None
    supporting_metric_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: str = "medium"        # high | medium | low
    evidence_quality: str = "metric_backed"
    validation_status: str = "validated"  # validated | unsupported | rejected
    validation_notes: list[str] = Field(default_factory=list)
    source: str = "llm"               # llm | deterministic_fallback
    model: str | None = None
    prompt_version: str | None = None
    created_at: str | None = None

    def to_mongo(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


# ── Structured response shapes the LLM must return per section ──────────────

class SectionInsightOut(BaseModel):
    """Generic section narrative. ``points`` are individual findings; each cites
    the metric ids and evidence ids it rests on."""
    model_config = ConfigDict(extra="forbid")
    headline: str
    points: list["SectionPoint"] = Field(default_factory=list)


class SectionPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    observation: str
    business_implication: str | None = None
    supporting_metric_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class RecommendationsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recommendations: list[Recommendation] = Field(default_factory=list)


SectionInsightOut.model_rebuild()
