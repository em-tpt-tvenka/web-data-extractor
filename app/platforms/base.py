from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

@dataclass
class ScrapeResult:
    raw_products: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_meta: dict[str, Any] = field(default_factory=dict)

class PlatformAdapter(ABC):
    name: str

    @abstractmethod
    def config_schema(self) -> PlatformConfigSchema: ...

    @abstractmethod
    def validate_options(self, raw: dict[str, Any]) -> Any: ...

    def validate_request(self, request: ScrapeJobRequest) -> None:
        """Optional platform checks on the common request fields (e.g. that the
        marketplace is one this platform serves). Raise ValueError to reject the
        job before it is queued. Default: no extra checks."""

    @abstractmethod
    async def scrape(
        self, request: ScrapeJobRequest, options: Any, run_id: str
    ) -> ScrapeResult: ...

    @abstractmethod
    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]: ...
