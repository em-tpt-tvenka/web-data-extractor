from __future__ import annotations

from typing import Any

from app.core.exceptions import PlatformNotImplementedError
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest


class PlaceholderAdapter(PlatformAdapter):
    name = "placeholder"

    def config_schema(self) -> PlatformConfigSchema:
        return PlatformConfigSchema(platform=self.name, fields=[])

    def validate_options(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw or {}

    async def scrape(
        self, request: ScrapeJobRequest, options: Any, run_id: str
    ) -> ScrapeResult:
        raise PlatformNotImplementedError(
            f"scraping for platform '{self.name}' is not implemented yet"
        )

    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]:
        return []
