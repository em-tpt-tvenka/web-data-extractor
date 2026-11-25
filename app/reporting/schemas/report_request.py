"""API request models for the reporting endpoints."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator

from app.reporting.constants import OutputFormat, ReportType, SummaryType


class ReportDateRange(BaseModel):
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def _order(self) -> "ReportDateRange":
        if self.start and self.end and self.start > self.end:
            raise ValueError("start cannot be after end")
        return self


class SummaryFiltersIn(BaseModel):
    """Filters shared by summary creation and inline report creation.

    NOTE: there is no authentication layer in this service, so ``tenant_id`` /
    ``project_id`` are optional scoping filters, not an identity. When omitted,
    the summary spans all stored data (single-tenant deployments). A real
    deployment must resolve these from the authenticated principal.
    """
    tenant_id: str | None = None
    project_id: str | None = None
    platforms: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    date_range: ReportDateRange | None = None


class SummaryRequest(SummaryFiltersIn):
    summary_type: SummaryType = SummaryType.competitive_analysis


class ReportRequest(SummaryFiltersIn):
    # Either reuse an existing summary by id, or let filters build one.
    summary_id: str | None = None
    summary_type: SummaryType = SummaryType.competitive_analysis
    report_type: ReportType = ReportType.competitive_analysis
    title: str | None = None
    include_sections: list[str] | None = None
    output_formats: list[OutputFormat] = Field(
        default_factory=lambda: [OutputFormat.pdf, OutputFormat.xlsx]
    )
    # Overrides settings.llm_provider for this report only.
    llm_provider: str | None = None
