"""End-to-end report orchestration.

Summary generation and report generation are separate internally: a report
reuses a compatible stored summary when possible, otherwise builds one. The
pipeline advances an explicit status (``report_repository``) so a failure in one
stage (e.g. PDF) does not discard completed work, and a report is always
produced — with a deterministic narrative if no LLM is available.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core import identifiers
from app.core.config import settings
from app.core.exceptions import ReportNotFoundError, SummaryNotFoundError
from app.core.logging import get_logger
from app.reporting.constants import ALL_SECTIONS, OutputFormat, ReportStatus
from app.reporting.generators.chart_generator import ChartGenerator
from app.reporting.generators.excel_generator import ExcelReportGenerator
from app.reporting.generators.pdf_generator import PdfReportGenerator
from app.reporting.repositories.artifact_repository import ArtifactRepository
from app.reporting.repositories.indexes import ensure_reporting_indexes
from app.reporting.repositories.insight_repository import InsightRepository
from app.reporting.repositories.report_repository import ReportRepository
from app.reporting.repositories.summary_repository import SummaryRepository
from app.reporting.schemas.report_request import ReportRequest, SummaryFiltersIn
from app.reporting.schemas.report_summary import ReportSummary
from app.reporting.services.insight_service import InsightService
from app.reporting.services.report_summary_service import ReportSummaryService
from app.reporting.utils.report_helpers import now_iso

_log = get_logger("reporting.report")
_INLINE_TASKS: set[asyncio.Task] = set()


class ReportService:
    def __init__(self) -> None:
        self.reports = ReportRepository()
        self.summaries = SummaryRepository()
        self.insights_repo = InsightRepository()
        self.artifacts = ArtifactRepository()
        self.summary_service = ReportSummaryService()
        self.insight_service = InsightService()

    # ── creation / dispatch ────────────────────────────────────────────────

    async def create_report(self, request: ReportRequest) -> dict:
        await ensure_reporting_indexes()
        report_id = identifiers.report_id()
        sections = request.include_sections or ALL_SECTIONS
        doc = {
            "_id": report_id,
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
            "report_type": request.report_type.value,
            "status": ReportStatus.pending.value,
            "summary_id": request.summary_id,
            "request": request.model_dump(mode="json"),
            "include_sections": sections,
            "output_formats": [f.value for f in request.output_formats],
            "llm": {"provider": (request.llm_provider or settings.llm_provider)},
            "artifacts": {},
            "insight_count": 0,
            "unsupported_sections": {},
            "error": None,
            "created_at": now_iso(),
            "completed_at": None,
        }
        await self.reports.create(doc)
        self._dispatch(report_id)
        _log.info("event=report_created report_id=%s executor=%s",
                  report_id, settings.job_executor)
        return doc

    def _dispatch(self, report_id: str) -> None:
        if settings.job_executor == "inline":
            task = asyncio.create_task(self._run_safe(report_id))
            _INLINE_TASKS.add(task)
            task.add_done_callback(_INLINE_TASKS.discard)
            return
        from app.reporting.workers.report_tasks import run_report_task
        run_report_task.delay(report_id)

    async def _run_safe(self, report_id: str) -> None:
        try:
            await self.run_report(report_id)
        except Exception:
            _log.exception("event=report_crashed report_id=%s", report_id)

    # ── pipeline ────────────────────────────────────────────────────────────

    async def run_report(self, report_id: str) -> None:
        await ensure_reporting_indexes()
        doc = await self.reports.get(report_id)
        if doc is None:
            raise ReportNotFoundError(f"report '{report_id}' not found")
        request = ReportRequest.model_validate(doc["request"])

        try:
            summary = await self._resolve_summary(report_id, request)
            await self.summaries.set_report_id(summary.id, report_id)
            await self.reports.set_status(
                report_id, ReportStatus.summary_saved.value,
                summary_id=summary.id,
                unsupported_sections=summary.unsupported_sections,
            )

            if summary.data_coverage.total_reviews == 0:
                await self.reports.set_status(
                    report_id, ReportStatus.failed.value,
                    error="no reviews matched the requested filters",
                    completed_at=now_iso())
                _log.warning("event=report_no_data report_id=%s", report_id)
                return

            sections = request.include_sections or ALL_SECTIONS
            await self.reports.set_status(report_id, ReportStatus.llm_generating.value)
            gen = await self.insight_service.generate(
                report_id, summary, sections, llm_provider=request.llm_provider)

            await self.reports.set_status(report_id, ReportStatus.generating_files.value)
            artifacts, failures = await self._generate_files(report_id, request, summary, gen.insights)

            status = (ReportStatus.partially_completed if failures
                      else ReportStatus.completed).value
            await self.reports.update(
                report_id,
                status=status,
                artifacts=artifacts,
                insight_count=len(gen.insights),
                llm={"provider": gen.provider_label,
                     "sections_via_llm": gen.sections_via_llm,
                     "sections_via_fallback": gen.sections_via_fallback},
                error="; ".join(failures) or None,
                completed_at=now_iso(),
            )
            _log.info("event=report_done report_id=%s status=%s artifacts=%s",
                      report_id, status, list(artifacts))
        except Exception as exc:
            await self.reports.set_status(
                report_id, ReportStatus.failed.value,
                error=str(exc), completed_at=now_iso())
            _log.exception("event=report_failed report_id=%s", report_id)
            raise

    async def _resolve_summary(self, report_id: str, request: ReportRequest) -> ReportSummary:
        if request.summary_id:
            existing = await self.summaries.get(request.summary_id)
            if existing is None:
                raise SummaryNotFoundError(f"summary '{request.summary_id}' not found")
            return ReportSummary.model_validate(existing)
        filters = SummaryFiltersIn(
            tenant_id=request.tenant_id, project_id=request.project_id,
            platforms=request.platforms, brands=request.brands,
            categories=request.categories, date_range=request.date_range,
        )
        summary, _reused = await self.summary_service.get_or_create(
            filters, request.summary_type.value)
        return summary

    async def _generate_files(self, report_id, request, summary, insights):
        out_dir = Path(settings.report_export_dir) / report_id
        insight_dicts = [i.model_dump() for i in insights]
        formats = {f.value for f in request.output_formats}
        artifacts: dict[str, str] = {}
        failures: list[str] = []

        charts: dict[str, bytes] = {}
        if OutputFormat.pdf.value in formats:
            try:
                charts = await asyncio.to_thread(ChartGenerator().build_all, summary)
            except Exception as exc:  # pragma: no cover
                _log.warning("event=charts_failed report_id=%s error=%s", report_id, exc)

        if OutputFormat.xlsx.value in formats:
            try:
                path = await asyncio.to_thread(
                    ExcelReportGenerator().generate, summary, insight_dicts,
                    out_dir / "report.xlsx")
                artifacts["xlsx"] = path
                await self._save_artifact(report_id, request, "xlsx", path)
            except Exception as exc:
                failures.append(f"excel: {exc}")
                _log.exception("event=excel_failed report_id=%s", report_id)

        if OutputFormat.pdf.value in formats:
            pdf = PdfReportGenerator()
            if not pdf.available:
                failures.append("pdf: ReportLab not installed")
            else:
                try:
                    title = request.title or "Retail Review Analytics Report"
                    path = await asyncio.to_thread(
                        pdf.generate, summary, insight_dicts, charts,
                        out_dir / "report.pdf", title)
                    artifacts["pdf"] = path
                    await self._save_artifact(report_id, request, "pdf", path)
                except Exception as exc:
                    failures.append(f"pdf: {exc}")
                    _log.exception("event=pdf_failed report_id=%s", report_id)

        return artifacts, failures

    async def _save_artifact(self, report_id, request, file_type, path) -> None:
        p = Path(path)
        await self.artifacts.save({
            "_id": identifiers.artifact_id(),
            "report_id": report_id,
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
            "file_type": file_type,
            "path": str(p),
            "size_bytes": p.stat().st_size if p.exists() else None,
            "status": "available" if p.exists() else "missing",
            "created_at": now_iso(),
        })

    # ── regenerate ──────────────────────────────────────────────────────────

    async def regenerate(self, report_id: str) -> dict:
        doc = await self.reports.get(report_id)
        if doc is None:
            raise ReportNotFoundError(f"report '{report_id}' not found")
        await self.insights_repo.delete_for_report(report_id)
        await self.reports.set_status(report_id, ReportStatus.pending.value,
                                      error=None, completed_at=None)
        self._dispatch(report_id)
        return await self.reports.get(report_id)
