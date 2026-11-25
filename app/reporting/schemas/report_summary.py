"""The ``report_summaries`` document: the aggregated, Python-computed analytics
that are the single source of truth for everything downstream (LLM narrative,
PDF, Excel). It never contains raw review text in bulk — only metrics plus a
bounded set of evidence references."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.reporting.schemas.report_metrics import (
    BrandMetric,
    CompetitiveInsight,
    ComplaintMetric,
    DataCoverage,
    DataQuality,
    PlatformMetric,
    ProductMetric,
    RiskItem,
    SentimentSummary,
    TopicMetric,
    TrendSummary,
)


class SummaryFilters(BaseModel):
    model_config = ConfigDict(extra="allow")
    tenant_id: str | None = None
    project_id: str | None = None
    platforms: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None


class EvidenceRef(BaseModel):
    """A pointer to a representative review kept as evidence. Stores an id and a
    short snippet only — not the whole review — so summaries stay small and the
    original ``reviews`` collection remains the system of record."""
    model_config = ConfigDict(extra="allow")
    review_id: str
    source_record_id: str | None = None
    platform: str | None = None
    product_id: str | None = None
    brand: str | None = None
    rating: float | None = None
    sentiment: str | None = None
    snippet: str | None = None
    section: str | None = None


class ReportSummary(BaseModel):
    """Full ``report_summaries`` document."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="_id")
    tenant_id: str | None = None
    project_id: str | None = None
    report_id: str | None = None
    summary_type: str = "competitive_analysis"
    aggregation_version: str = "1.0"
    summary_key: str | None = None  # idempotency / reuse key

    filters: SummaryFilters = Field(default_factory=SummaryFilters)
    data_coverage: DataCoverage = Field(default_factory=DataCoverage)
    data_quality: DataQuality = Field(default_factory=DataQuality)

    sentiment_summary: SentimentSummary = Field(default_factory=SentimentSummary)
    brand_metrics: list[BrandMetric] = Field(default_factory=list)
    topic_summary: list[TopicMetric] = Field(default_factory=list)
    complaint_summary: list[ComplaintMetric] = Field(default_factory=list)
    risk_summary: list[RiskItem] = Field(default_factory=list)
    platform_summary: list[PlatformMetric] = Field(default_factory=list)
    product_summary: list[ProductMetric] = Field(default_factory=list)
    monthly_trends: TrendSummary = Field(default_factory=TrendSummary)
    competitive_insights: list[CompetitiveInsight] = Field(default_factory=list)

    # Flat {metric_id: value} map used by the response validator to confirm that
    # any number an LLM cites actually exists and matches.
    metric_index: dict[str, Any] = Field(default_factory=dict)

    evidence: list[EvidenceRef] = Field(default_factory=list)
    unsupported_sections: dict[str, str] = Field(default_factory=dict)

    source_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def to_mongo(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)
