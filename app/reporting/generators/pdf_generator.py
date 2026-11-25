"""PDF report via ReportLab Platypus. Imported lazily; ``available`` is False
when ReportLab is not installed (the service then still produces Excel)."""

from __future__ import annotations

import io
from pathlib import Path

from app.core.logging import get_logger
from app.reporting.constants import (
    PALETTE,
    SECTION_BRAND_COMPARISON,
    SECTION_CATEGORY,
    SECTION_COMPLAINT,
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_RECOMMENDATIONS,
    SECTION_RISK,
    SECTION_SENTIMENT,
)
from app.reporting.schemas.report_summary import ReportSummary

_log = get_logger("reporting.pdf")


class PdfReportGenerator:
    def __init__(self) -> None:
        try:
            self._load()
            self.available = True
        except Exception as exc:  # pragma: no cover
            _log.warning("event=reportlab_unavailable error=%s", exc)
            self.available = False

    def _load(self) -> None:
        from reportlab.lib import colors  # noqa: PLC0415
        from reportlab.lib.pagesizes import A4  # noqa: PLC0415
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: PLC0415
        from reportlab.lib.units import inch, mm  # noqa: PLC0415
        self._colors = colors
        self._A4 = A4
        self._inch = inch
        self._mm = mm
        base = getSampleStyleSheet()
        ink = colors.HexColor(PALETTE["ink"])
        self._styles = {
            "title": ParagraphStyle("t", parent=base["Title"], textColor=ink, fontSize=24),
            "h2": ParagraphStyle("h2", parent=base["Heading2"], textColor=ink),
            "h3": ParagraphStyle("h3", parent=base["Heading3"], textColor=ink),
            "body": ParagraphStyle("b", parent=base["BodyText"], fontSize=9, leading=13),
            "small": ParagraphStyle("s", parent=base["BodyText"], fontSize=7.5,
                                    textColor=colors.HexColor("#5b6472")),
            "kpi": ParagraphStyle("k", parent=base["BodyText"], fontSize=20, textColor=ink),
        }

    # ── public ───────────────────────────────────────────────────────────

    def generate(self, summary: ReportSummary, insights: list[dict],
                 charts: dict[str, bytes], path: str | Path, title: str) -> str:
        if not self.available:
            raise RuntimeError("ReportLab is not installed")
        from reportlab.platypus import (  # noqa: PLC0415
            Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        by_section: dict[str, list[dict]] = {}
        for i in insights:
            by_section.setdefault(i.get("section"), []).append(i)

        S = self._styles
        inch = self._inch
        story: list = []
        sec = _Counter()

        def img(name, w=4.6, h=3.0):
            data = charts.get(name)
            if data:
                story.append(Image(io.BytesIO(data), width=w * inch, height=h * inch))
                story.append(Spacer(1, 6))

        def para(text, style="body"):
            story.append(Paragraph(text, S[style]))

        def heading(text):
            story.append(Paragraph(f"{sec.next()}. {text}", S["h2"]))
            story.append(Spacer(1, 4))

        def points(section):
            for p in by_section.get(section, []):
                flag = "" if p.get("validation_status") == "validated" else " [unsupported]"
                para(f"<b>{_esc(p.get('title'))}</b>{flag}", "body")
                para(_esc(p.get("observation")), "body")
                if p.get("business_implication"):
                    para(f"<i>So what:</i> {_esc(p['business_implication'])}", "small")
                story.append(Spacer(1, 4))

        # Cover
        cov = summary.data_coverage
        para(title, "title")
        story.append(Spacer(1, 10))
        para("Retail Review Analytics Report", "h3")
        para(f"Generated from {cov.total_reviews} marketplace reviews across "
             f"{cov.platforms} platform(s) and {cov.brands} brand(s).", "body")
        para(f"Date range: {summary.filters.start_date or 'all'} to "
             f"{summary.filters.end_date or 'all'}.", "small")
        para("All figures are computed in Python from scraped reviews; narrative "
             "is validated against those figures.", "small")
        story.append(PageBreak())

        # 1. Scope & coverage
        heading("Report scope & coverage")
        scope_rows = [
            ["Reviews analysed", str(cov.total_reviews)],
            ["Unique reviews", str(cov.unique_reviews)],
            ["Products", str(cov.total_products)],
            ["Brands", str(cov.brands)],
            ["Platforms", str(cov.platforms)],
            ["With rating / text / date",
             f"{cov.reviews_with_rating} / {cov.reviews_with_text} / {cov.reviews_with_date}"],
        ]
        story.append(self._kv_table(scope_rows))
        story.append(Spacer(1, 8))

        # 2. Executive summary
        heading("Executive summary")
        points(SECTION_EXECUTIVE_SUMMARY)

        # 3. KPI scorecard
        heading("KPI scorecard")
        ss = summary.sentiment_summary
        story.append(self._scorecard([
            ("Net sentiment", str(ss.net_sentiment)),
            ("Positive %", str(ss.positive_pct)),
            ("Negative %", str(ss.negative_pct)),
            ("Avg rating", str(ss.average_rating)),
        ]))
        img("sentiment_donut", 3.8, 2.9)

        # 4. Brand comparison
        heading("Brand comparison")
        if summary.brand_metrics:
            img("share_of_voice", 3.8, 2.9)
            img("brand_net_sentiment", 5.0, 3.0)
            story.append(self._brand_table(summary))
        points(SECTION_BRAND_COMPARISON)
        story.append(PageBreak())

        # 5. Sentiment
        heading("Sentiment analysis")
        img("monthly_trend", 5.4, 2.8)
        points(SECTION_SENTIMENT)

        # 6. Category / topics
        heading("Category & topic insights")
        img("topic_bar", 5.0, 3.0)
        points(SECTION_CATEGORY)
        story.append(PageBreak())

        # 7. Complaints
        heading("Complaint analysis")
        img("complaint_bar", 5.0, 2.9)
        points(SECTION_COMPLAINT)

        # 8. Risk
        heading("Risk & crisis watch")
        points(SECTION_RISK)

        # 9. Recommendations
        heading("Recommendations")
        story.append(self._reco_table(by_section.get(SECTION_RECOMMENDATIONS, [])))
        story.append(PageBreak())

        # 10. Appendix
        heading("Appendix: methodology, definitions & data caveats")
        for note in summary.data_quality.notes:
            para("• " + _esc(note), "small")
        para("<b>Unsupported sections (no backing data):</b>", "small")
        for name, need in summary.unsupported_sections.items():
            para(f"• {name}: requires {need}", "small")
        para("<b>KPI definitions:</b> Net sentiment = positive% − negative% over "
             "classified reviews. Share of voice = brand mentions / branded "
             "mentions. Complaints counted on negative/mixed reviews only.", "small")

        doc = SimpleDocTemplate(
            str(path), pagesize=self._A4,
            topMargin=18 * self._mm, bottomMargin=16 * self._mm,
            leftMargin=16 * self._mm, rightMargin=16 * self._mm,
            title=title,
        )
        doc.build(story, onFirstPage=self._footer, onLaterPages=self._footer)
        return str(path)

    # ── helpers ───────────────────────────────────────────────────────────

    def _footer(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(self._colors.HexColor("#8a93a3"))
        canvas.drawString(16 * self._mm, 9 * self._mm,
                          "web-data-extractor · Retail Review Analytics · figures computed in Python")
        canvas.drawRightString(200 * self._mm, 9 * self._mm, f"Page {doc.page}")
        canvas.restoreState()

    def _table_style(self):
        from reportlab.platypus import TableStyle  # noqa: PLC0415
        c = self._colors
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), c.HexColor(PALETTE["ink"])),
            ("TEXTCOLOR", (0, 0), (-1, 0), c.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.3, c.HexColor("#d4d9e0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [c.white, c.HexColor("#f4f6f9")]),
        ])

    def _kv_table(self, rows):
        from reportlab.platypus import Table  # noqa: PLC0415
        t = Table([["Metric", "Value"], *rows], colWidths=[2.4 * self._inch, 3.6 * self._inch])
        t.setStyle(self._table_style())
        return t

    def _scorecard(self, cells):
        from reportlab.platypus import Paragraph, Table, TableStyle  # noqa: PLC0415
        c = self._colors
        row = [Paragraph(f"<b>{v}</b><br/><font size=7>{k}</font>", self._styles["body"])
               for k, v in cells]
        t = Table([row], colWidths=[1.5 * self._inch] * len(cells))
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.4, c.HexColor("#d4d9e0")),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, c.HexColor("#d4d9e0")),
            ("BACKGROUND", (0, 0), (-1, -1), c.HexColor("#f4f6f9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return t

    def _brand_table(self, summary):
        from reportlab.platypus import Table  # noqa: PLC0415
        head = ["Brand", "Mentions", "SoV %", "Net sent.", "Pos %", "Neg %", "Rating"]
        rows = [[b.brand, b.mentions, b.share_of_voice_pct, b.net_sentiment,
                 b.positive_pct, b.negative_pct, b.average_rating]
                for b in summary.brand_metrics[:12]]
        t = Table([head, *rows])
        t.setStyle(self._table_style())
        return t

    def _reco_table(self, recs):
        from reportlab.platypus import Paragraph, Table  # noqa: PLC0415
        head = ["Area", "Recommendation", "Pri", "Impact", "Effort"]
        rows = []
        for r in recs:
            rows.append([
                r.get("area", ""),
                Paragraph(_esc(r.get("recommendation", "")), self._styles["small"]),
                r.get("priority", ""), r.get("impact", ""), r.get("effort", ""),
            ])
        if not rows:
            rows = [["—", Paragraph("No recommendations generated.", self._styles["small"]),
                     "", "", ""]]
        t = Table([head, *rows],
                  colWidths=[1.1 * self._inch, 3.0 * self._inch, 0.5 * self._inch,
                             0.7 * self._inch, 0.7 * self._inch])
        t.setStyle(self._table_style())
        return t


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
