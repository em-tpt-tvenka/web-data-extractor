"""Reporting API.

NOTE on isolation: this service has no authentication layer, so tenant/project
are optional scoping filters supplied by the caller rather than an authenticated
identity. Where a ``tenant_id``/``project_id`` query param is given on a read, it
must match the stored report or the resource is treated as not found — the hook a
real auth layer would enforce. Local artifact paths are never returned to the
caller; files are streamed through the download endpoints only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Query, status
from fastapi.responses import FileResponse

from app.core.exceptions import (
    ArtifactNotFoundError,
    ReportNotFoundError,
    SummaryNotFoundError,
)
from app.reporting.constants import (
    ALL_SECTIONS,
    UNSUPPORTED_SECTIONS,
    OutputFormat,
    ReportType,
    SummaryType,
)
from app.reporting.repositories.artifact_repository import ArtifactRepository
from app.reporting.repositories.insight_repository import InsightRepository
from app.reporting.repositories.report_repository import ReportRepository
from app.reporting.repositories.summary_repository import SummaryRepository
from app.reporting.schemas.report_request import ReportRequest, SummaryFiltersIn, SummaryRequest
from app.reporting.schemas.report_response import (
    ReportListResponse,
    ReportResponse,
    SummaryCreatedResponse,
)
from app.reporting.services.report_service import ReportService
from app.reporting.services.report_summary_service import ReportSummaryService

router = APIRouter(prefix="/reports", tags=["reports"])

_reports = ReportRepository()
_summaries = SummaryRepository()
_insights = InsightRepository()
_artifacts = ArtifactRepository()
_report_service = ReportService()
_summary_service = ReportSummaryService()


def _to_response(doc: dict) -> ReportResponse:
    return ReportResponse(
        report_id=doc["_id"],
        tenant_id=doc.get("tenant_id"),
        project_id=doc.get("project_id"),
        report_type=doc.get("report_type"),
        status=doc.get("status"),
        summary_id=doc.get("summary_id"),
        include_sections=doc.get("include_sections", []),
        output_formats=doc.get("output_formats", []),
        llm=doc.get("llm", {}),
        artifacts={k: f"/api/v1/reports/{doc['_id']}/download/{ 'excel' if k=='xlsx' else k }"
                   for k in (doc.get("artifacts") or {})},
        insight_count=doc.get("insight_count", 0),
        unsupported_sections=doc.get("unsupported_sections", {}),
        error=doc.get("error"),
        created_at=doc.get("created_at"),
        completed_at=doc.get("completed_at"),
    )


def _check_scope(doc: dict, tenant_id: str | None, project_id: str | None) -> None:
    if tenant_id is not None and doc.get("tenant_id") not in (None, tenant_id):
        raise ReportNotFoundError("report not found in this tenant scope")
    if project_id is not None and doc.get("project_id") not in (None, project_id):
        raise ReportNotFoundError("report not found in this project scope")


async def _load_report(report_id: str, tenant_id=None, project_id=None) -> dict:
    doc = await _reports.get(report_id)
    if doc is None:
        raise ReportNotFoundError(f"report '{report_id}' not found")
    _check_scope(doc, tenant_id, project_id)
    return doc


# ── summaries ──────────────────────────────────────────────────────────────

@router.post("/summaries", response_model=SummaryCreatedResponse,
             status_code=status.HTTP_201_CREATED)
async def create_summary(request: SummaryRequest) -> SummaryCreatedResponse:
    filters = SummaryFiltersIn(
        tenant_id=request.tenant_id, project_id=request.project_id,
        platforms=request.platforms, brands=request.brands,
        categories=request.categories, date_range=request.date_range,
    )
    summary, reused = await _summary_service.get_or_create(filters, request.summary_type.value)
    return SummaryCreatedResponse(
        summary_id=summary.id, summary_type=summary.summary_type, reused=reused,
        data_coverage=summary.data_coverage.model_dump(), created_at=summary.created_at,
    )


@router.get("/summaries/{summary_id}")
async def get_summary(summary_id: str) -> dict:
    doc = await _summaries.get(summary_id)
    if doc is None:
        raise SummaryNotFoundError(f"summary '{summary_id}' not found")
    return doc


# ── config schema (declared BEFORE /{report_id} so it is not shadowed) ──────

@router.get("/config-schema")
async def report_config_schema() -> dict:
    return {
        "summary_types": [t.value for t in SummaryType],
        "report_types": [t.value for t in ReportType],
        "sections": ALL_SECTIONS,
        "output_formats": [f.value for f in OutputFormat],
        "llm_providers": ["fallback", "ollama", "gemini", "openrouter"],
        "unsupported_sections": UNSUPPORTED_SECTIONS,
        "notes": "tenant_id/project_id are optional scoping filters (no auth layer).",
    }


# ── reports ─────────────────────────────────────────────────────────────────

@router.post("", response_model=ReportResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_report(request: ReportRequest) -> ReportResponse:
    doc = await _report_service.create_report(request)
    return _to_response(doc)


@router.get("", response_model=ReportListResponse)
async def list_reports(
    tenant_id: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ReportListResponse:
    docs = await _reports.list(tenant_id, project_id, limit)
    return ReportListResponse(reports=[_to_response(d) for d in docs], count=len(docs))


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    tenant_id: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
) -> ReportResponse:
    return _to_response(await _load_report(report_id, tenant_id, project_id))


@router.get("/{report_id}/insights")
async def get_report_insights(report_id: str) -> dict:
    await _load_report(report_id)
    rows = await _insights.for_report(report_id)
    return {"report_id": report_id, "count": len(rows), "insights": rows}


@router.get("/{report_id}/metrics")
async def get_report_metrics(report_id: str) -> dict:
    doc = await _load_report(report_id)
    summary_id = doc.get("summary_id")
    summary = await _summaries.get(summary_id) if summary_id else None
    if summary is None:
        return {"report_id": report_id, "summary_id": summary_id, "metrics": {}}
    return {
        "report_id": report_id,
        "summary_id": summary_id,
        "metric_index": summary.get("metric_index", {}),
        "sentiment_summary": summary.get("sentiment_summary", {}),
        "brand_metrics": summary.get("brand_metrics", []),
        "topic_summary": summary.get("topic_summary", []),
        "complaint_summary": summary.get("complaint_summary", []),
        "risk_summary": summary.get("risk_summary", []),
        "data_coverage": summary.get("data_coverage", {}),
        "data_quality": summary.get("data_quality", {}),
    }


@router.post("/{report_id}/regenerate", response_model=ReportResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def regenerate_report(report_id: str) -> ReportResponse:
    await _load_report(report_id)
    return _to_response(await _report_service.regenerate(report_id))


async def _download(report_id: str, file_type: str, media_type: str, filename: str):
    await _load_report(report_id)
    art = await _artifacts.get_by_type(report_id, file_type)
    if art is None or not art.get("path") or not Path(art["path"]).exists():
        raise ArtifactNotFoundError(f"{file_type} artifact not available for '{report_id}'")
    return FileResponse(art["path"], media_type=media_type, filename=filename)


@router.get("/{report_id}/download/pdf")
async def download_pdf(report_id: str):
    return await _download(report_id, "pdf", "application/pdf", f"{report_id}.pdf")


@router.get("/{report_id}/download/excel")
async def download_excel(report_id: str):
    return await _download(
        report_id, "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"{report_id}.xlsx")
