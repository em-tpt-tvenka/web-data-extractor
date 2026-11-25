"""Read-side helper for recommendation insights.

Recommendations are produced by :mod:`insight_service` (the ``recommendations``
section). This service exposes them for the API without recomputing anything.
"""

from __future__ import annotations

from app.reporting.constants import SECTION_RECOMMENDATIONS
from app.reporting.repositories.insight_repository import InsightRepository


class RecommendationService:
    def __init__(self) -> None:
        self.repo = InsightRepository()

    async def for_report(self, report_id: str) -> list[dict]:
        rows = await self.repo.for_report(report_id)
        recs = [r for r in rows if r.get("section") == SECTION_RECOMMENDATIONS]
        # Validated, highest-priority first.
        order = {"P1": 0, "P2": 1, "P3": 2}
        recs.sort(key=lambda r: order.get(r.get("priority"), 3))
        return recs
