"""Build and persist a ``report_summaries`` document from enriched reviews.

Summary generation is a standalone operation: the resulting document is the sole
input to every downstream report, and a compatible summary is reused rather than
recomputed (keyed on filters + aggregation version + data snapshot signature).
"""

from __future__ import annotations

import hashlib

from app.core import identifiers
from app.core.config import settings
from app.core.logging import get_logger
from app.reporting import AGGREGATION_VERSION
from app.reporting.analytics import (
    brand_analytics,
    competitor_analytics,
    complaint_analytics,
    kpi_calculator,
    risk_detection,
    sentiment_analytics,
    topic_analytics,
    trend_analytics,
)
from app.reporting.constants import UNSUPPORTED_SECTIONS
from app.reporting.repositories.indexes import ensure_reporting_indexes
from app.reporting.repositories.summary_repository import SummaryRepository
from app.reporting.schemas.report_request import SummaryFiltersIn
from app.reporting.schemas.report_summary import ReportSummary, SummaryFilters
from app.reporting.services.report_data_service import DataBundle, ReportDataService
from app.reporting.utils.evidence_selector import select_evidence
from app.reporting.utils.report_helpers import now_iso

_log = get_logger("reporting.summary")


def _summary_key(filters: SummaryFiltersIn, summary_type: str, snapshot: str) -> str:
    basis = f"{summary_type}|{filters.model_dump_json()}|{AGGREGATION_VERSION}|{snapshot}"
    return "key:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


class ReportSummaryService:
    def __init__(self) -> None:
        self.data = ReportDataService()
        self.repo = SummaryRepository()

    async def get_or_create(
        self, filters: SummaryFiltersIn, summary_type: str, *, allow_reuse: bool = True
    ) -> tuple[ReportSummary, bool]:
        """Return ``(summary, reused)``. Reuses a compatible stored summary when
        ``report_reuse_summaries`` is on and the snapshot matches."""
        await ensure_reporting_indexes()
        bundle = await self.data.fetch(filters)
        key = _summary_key(filters, summary_type, bundle.snapshot_signature)

        if allow_reuse and settings.report_reuse_summaries:
            existing = await self.repo.find_reusable(key, filters.tenant_id, filters.project_id)
            if existing:
                _log.info("event=summary_reused summary_id=%s", existing["_id"])
                return ReportSummary.model_validate(existing), True

        summary = self._build(filters, summary_type, bundle, key)
        await self.repo.save(summary)
        _log.info("event=summary_created summary_id=%s reviews=%d",
                  summary.id, summary.data_coverage.total_reviews)
        return summary, False

    # ── assembly ────────────────────────────────────────────────────────────

    def _build(
        self, filters: SummaryFiltersIn, summary_type: str, bundle: DataBundle, key: str
    ) -> ReportSummary:
        reviews = bundle.reviews

        coverage = kpi_calculator.data_coverage(reviews, bundle.product_count)
        quality = kpi_calculator.data_quality(reviews)
        sentiment = kpi_calculator.sentiment_summary(reviews)

        brands, brand_idx = brand_analytics.brand_metrics(reviews)
        topics, topic_idx = topic_analytics.topic_summary(reviews)
        complaints, complaint_idx = complaint_analytics.complaint_summary(reviews)
        risks, risk_idx = risk_detection.risk_summary(
            reviews,
            growth_threshold=settings.risk_growth_qoq_threshold,
            negative_share_threshold=settings.risk_negative_share_threshold,
            min_volume=settings.risk_min_volume,
        )
        platforms, platform_idx = sentiment_analytics.platform_summary(reviews)
        product_metrics, product_idx = sentiment_analytics.product_summary(reviews)
        trends, trend_idx = trend_analytics.monthly_trends(reviews)
        comp_insights = competitor_analytics.competitive_insights(brands)

        metric_index: dict = {}
        metric_index.update(kpi_calculator.metric_index(coverage, sentiment))
        for frag in (brand_idx, topic_idx, complaint_idx, risk_idx,
                     platform_idx, product_idx, trend_idx):
            metric_index.update(frag)

        evidence = select_evidence(reviews, settings.report_evidence_per_section)
        dr = filters.date_range
        now = now_iso()

        return ReportSummary(
            _id=identifiers.summary_id(),
            tenant_id=filters.tenant_id,
            project_id=filters.project_id,
            summary_type=summary_type,
            aggregation_version=AGGREGATION_VERSION,
            summary_key=key,
            filters=SummaryFilters(
                tenant_id=filters.tenant_id, project_id=filters.project_id,
                platforms=list(filters.platforms), brands=list(filters.brands),
                categories=list(filters.categories),
                start_date=dr.start.isoformat() if dr and dr.start else None,
                end_date=dr.end.isoformat() if dr and dr.end else None,
            ),
            data_coverage=coverage,
            data_quality=quality,
            sentiment_summary=sentiment,
            brand_metrics=brands,
            topic_summary=topics,
            complaint_summary=complaints,
            risk_summary=risks,
            platform_summary=platforms,
            product_summary=product_metrics,
            monthly_trends=trends,
            competitive_insights=comp_insights,
            metric_index=metric_index,
            evidence=evidence,
            unsupported_sections=dict(UNSUPPORTED_SECTIONS),
            source_metadata={
                "source_collections": ["reviews", "products"],
                "aggregation_version": AGGREGATION_VERSION,
                "snapshot": bundle.snapshot_meta,
            },
            created_at=now,
            updated_at=now,
        )
