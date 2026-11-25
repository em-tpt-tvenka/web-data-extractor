"""API response models for the reporting endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SummaryCreatedResponse(BaseModel):
    summary_id: str
    summary_type: str
    reused: bool = False
    data_coverage: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class ArtifactRef(BaseModel):
    artifact_id: str
    file_type: str
    path: str | None = None
    size_bytes: int | None = None
    status: str = "available"
    created_at: str | None = None


class ReportResponse(BaseModel):
    report_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    report_type: str
    status: str
    summary_id: str | None = None
    include_sections: list[str] = Field(default_factory=list)
    output_formats: list[str] = Field(default_factory=list)
    llm: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    insight_count: int = 0
    unsupported_sections: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    created_at: str | None = None
    completed_at: str | None = None


class ReportListResponse(BaseModel):
    reports: list[ReportResponse] = Field(default_factory=list)
    count: int = 0
