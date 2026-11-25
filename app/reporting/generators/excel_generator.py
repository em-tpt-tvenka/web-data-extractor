"""Multi-sheet Excel workbook for a report (openpyxl, already a project dep)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.reporting.constants import SECTION_EXECUTIVE_SUMMARY, SECTION_RECOMMENDATIONS
from app.reporting.schemas.report_summary import ReportSummary

_HEADER_FILL = PatternFill("solid", fgColor="1F2430")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_TITLE_FONT = Font(bold=True, size=14, color="1F2430")
_WRAP = Alignment(wrap_text=True, vertical="top")


def _sheet(wb: Workbook, title: str):
    ws = wb.create_sheet(title=title[:31])
    return ws


def _table(ws, headers: list[str], rows: list[list[Any]], start_row: int = 1) -> int:
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=c, value=h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    for r, row in enumerate(rows, start=start_row + 1):
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            if isinstance(val, str) and len(val) > 40:
                cell.alignment = _WRAP
    # Column widths.
    for c, h in enumerate(headers, start=1):
        width = max(len(str(h)), *(len(str(row[c - 1])) for row in rows)) if rows else len(str(h))
        ws.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 10), 60)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{start_row + len(rows)}"
    return start_row + len(rows) + 2


class ExcelReportGenerator:
    def generate(self, summary: ReportSummary, insights: list[dict], path: str | Path) -> str:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        wb.remove(wb.active)  # drop the default sheet

        self._executive(wb, summary, insights)
        self._kpi(wb, summary)
        self._brands(wb, summary)
        self._sentiment(wb, summary)
        self._topics(wb, summary)
        self._products(wb, summary)
        self._platforms(wb, summary)
        self._complaints(wb, summary)
        self._recommendations(wb, insights)
        self._quality(wb, summary)

        wb.save(path)
        return str(path)

    # ── sheets ────────────────────────────────────────────────────────────

    def _executive(self, wb, s: ReportSummary, insights: list[dict]) -> None:
        ws = _sheet(wb, "Executive Summary")
        ws.cell(row=1, column=1, value="Executive Summary").font = _TITLE_FONT
        cov = s.data_coverage
        meta = [
            ["Reviews analysed", cov.total_reviews],
            ["Products", cov.total_products],
            ["Brands", cov.brands],
            ["Platforms", cov.platforms],
            ["Date range", f"{s.filters.start_date or 'all'} to {s.filters.end_date or 'all'}"],
            ["Net sentiment", s.sentiment_summary.net_sentiment],
        ]
        nxt = _table(ws, ["Metric", "Value"], meta, start_row=3)
        exec_points = [i for i in insights if i.get("section") == SECTION_EXECUTIVE_SUMMARY]
        rows = [[p.get("title"), p.get("observation"), p.get("business_implication"),
                 p.get("source"), p.get("validation_status")] for p in exec_points]
        _table(ws, ["Finding", "Observation", "Implication", "Source", "Status"], rows, start_row=nxt)

    def _kpi(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "KPI Metrics")
        rows = [[k, v] for k, v in sorted(s.metric_index.items())]
        _table(ws, ["metric_id", "value"], rows)

    def _brands(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Brand Comparison")
        rows = [[b.brand, b.mentions, b.share_of_voice_pct, b.net_sentiment,
                 b.positive_pct, b.negative_pct, b.average_rating] for b in s.brand_metrics]
        _table(ws, ["Brand", "Mentions", "Share of voice %", "Net sentiment",
                    "Positive %", "Negative %", "Avg rating"], rows)

    def _sentiment(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Sentiment Analysis")
        ss = s.sentiment_summary
        rows = [
            ["Positive", ss.positive, ss.positive_pct],
            ["Negative", ss.negative, ss.negative_pct],
            ["Neutral", ss.neutral, ss.neutral_pct],
            ["Mixed", ss.mixed, ss.mixed_pct],
            ["Unclassified", ss.unclassified, "-"],
            ["Net sentiment", ss.net_sentiment, "-"],
            ["Average rating", ss.average_rating, "-"],
        ]
        _table(ws, ["Class", "Count", "% of classified"], rows)

    def _topics(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Topic Analysis")
        rows = [[t.theme, t.category, t.mentions, t.share_pct, t.net_sentiment,
                 t.dominant_sentiment] for t in s.topic_summary]
        _table(ws, ["Theme", "Category", "Mentions", "Share %", "Net sentiment",
                    "Dominant"], rows)

    def _products(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Product Analysis")
        rows = [[p.product_name, p.brand, p.platform, p.reviews, p.net_sentiment,
                 p.average_rating] for p in s.product_summary]
        _table(ws, ["Product", "Brand", "Platform", "Reviews", "Net sentiment",
                    "Avg rating"], rows)

    def _platforms(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Platform Analysis")
        rows = [[p.platform, p.reviews, p.net_sentiment, p.positive_pct, p.negative_pct,
                 p.average_rating] for p in s.platform_summary]
        _table(ws, ["Platform", "Reviews", "Net sentiment", "Positive %", "Negative %",
                    "Avg rating"], rows)

    def _complaints(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Complaint Analysis")
        rows = [[c.category, c.count, c.share_of_complaints_pct] for c in s.complaint_summary]
        _table(ws, ["Category", "Count", "Share of complaints %"], rows)

    def _recommendations(self, wb, insights: list[dict]) -> None:
        ws = _sheet(wb, "Recommendations")
        recs = [i for i in insights if i.get("section") == SECTION_RECOMMENDATIONS]
        rows = [[r.get("title"), r.get("recommendation"), r.get("priority"),
                 r.get("impact"), r.get("effort"), r.get("success_kpi"),
                 ", ".join(r.get("supporting_metric_ids", [])), r.get("source")]
                for r in recs]
        _table(ws, ["Area", "Recommendation", "Priority", "Impact", "Effort",
                    "Success KPI", "Supporting metrics", "Source"], rows)

    def _quality(self, wb, s: ReportSummary) -> None:
        ws = _sheet(wb, "Data Quality")
        dq = s.data_quality
        rows = [
            ["Classified reviews", dq.classified_reviews],
            ["Unclassified reviews", dq.unclassified_reviews],
            ["Missing rating %", dq.missing_rating_pct],
            ["Missing text %", dq.missing_text_pct],
            ["Missing date %", dq.missing_date_pct],
            ["Missing brand %", dq.missing_brand_pct],
            ["Missing category %", dq.missing_category_pct],
        ]
        nxt = _table(ws, ["Check", "Value"], rows)
        notes = [[n] for n in dq.notes]
        notes += [["UNSUPPORTED: " + k + " — needs " + v]
                  for k, v in s.unsupported_sections.items()]
        _table(ws, ["Notes & unsupported sections"], notes, start_row=nxt)
