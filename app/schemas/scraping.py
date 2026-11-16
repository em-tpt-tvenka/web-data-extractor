from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

class Platform(str, Enum):
    alibaba = "alibaba"
    amazon = "amazon"
    flipkart = "flipkart"
    samsung = "samsung"
    bestbuy = "bestbuy"
    walmart = "walmart"

class SortBy(str, Enum):
    relevance = "relevance"
    price_asc = "price_asc"
    price_desc = "price_desc"
    rating = "rating"
    newest = "newest"

class DateRangeType(str, Enum):
    monthly = "monthly"
    quarterly = "quarterly"
    last_1_year = "last_1_year"
    last_2_years = "last_2_years"
    custom = "custom"

class DateRange(BaseModel):
    type: DateRangeType = DateRangeType.last_1_year
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _check_custom(self) -> DateRange:
        if self.type is DateRangeType.custom:
            if self.start_date is None or self.end_date is None:
                raise ValueError("custom date_range requires both start_date and end_date")
            if self.start_date > self.end_date:
                raise ValueError("start_date cannot be after end_date")
        return self

class SearchParams(BaseModel):
    product: str = Field(min_length=1)
    category: str | None = None
    brand: str | None = None
    keywords: list[str] = Field(default_factory=list)
    marketplace: str = Field(min_length=1)
    language: str | None = None

class CommonFilters(BaseModel):
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    exclude_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _price_order(self) -> CommonFilters:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price cannot be greater than max_price")
        return self

class ScrapingOptions(BaseModel):
    max_products: int = Field(default=20, ge=1, le=500)
    max_pages: int = Field(default=1, ge=0, le=50)
    sort_by: SortBy = SortBy.relevance
    review_pages_per_product: int = Field(default=1, ge=0, le=50)
    date_range: DateRange = Field(default_factory=DateRange)

class TrackingOptions(BaseModel):
    save_history: bool = True
    track_price_changes: bool = True
    track_availability_changes: bool = True
    track_rating_changes: bool = True
    track_review_changes: bool = True
    track_seller_changes: bool = True
    track_specification_changes: bool = True

DEFAULT_TRACKING = TrackingOptions()

_REQUEST_EXAMPLE = {
    "platform": "alibaba",
    "search": {
        "product": "stainless steel water bottle",
        "category": "Drinkware",
        "brand": "",
        "keywords": ["insulated", "750ml"],
        "marketplace": "alibaba.com",
        "language": "en",
    },
    "filters": {
        "min_price": 1,
        "max_price": 20,
        "currency": "USD",
        "exclude_keywords": ["sample", "used"],
    },
    "scraping_options": {
        "max_products": 20,
        "max_pages": 1,
        "sort_by": "relevance",
        "review_pages_per_product": 1,
        "date_range": {"type": "last_1_year", "start_date": None, "end_date": None},
    },
    "platform_options": {
        "alibaba": {
            "verified_supplier_only": True,
            "trade_assurance_only": False,
            "supplier_country": ["CN"],
            "supplier_type": "manufacturer",
            "include_sponsored": False,
        }
    },
}

class ScrapeJobRequest(BaseModel):
    platform: Platform
    search: SearchParams
    filters: CommonFilters = Field(default_factory=CommonFilters)
    scraping_options: ScrapingOptions = Field(default_factory=ScrapingOptions)
    platform_options: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Per-platform settings, keyed by platform name. Only the entry that "
            "matches `platform` is used; unknown platform keys are rejected. Fetch "
            "the valid keys for a platform from "
            "GET /api/v1/platforms/{platform}/config-schema."
        ),
    )

    @field_validator("platform_options")
    @classmethod
    def _options_for_known_platform(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        allowed = {p.value for p in Platform}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown platform_options keys: {sorted(unknown)}")
        return value

    def options_for_platform(self) -> dict[str, Any]:
        return self.platform_options.get(self.platform.value, {})

class JobStatus(str, Enum):
    pending = "pending"
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"

class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus
    platform: Platform
    created_at: datetime

class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    platform: Platform
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    products_scraped: int = 0
    records_written: int = 0
    exports: dict[str, str] | None = None
    execution_meta: dict[str, Any] | None = None
    error: str | None = None
