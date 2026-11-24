from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

from app.schemas.config_schema import ConfigField, FieldOption, FieldType, PlatformConfigSchema


@dataclass(frozen=True)
class Marketplace:
    domain: str
    name: str
    country_code: str
    currency: str
    locale: str
    accept_language: str
    timezone: str

    @property
    def base_url(self) -> str:
        return f"https://www.{self.domain}"


# English-language storefronts only: review dates, availability and delivery
# text are parsed from English page copy.
MARKETPLACES: dict[str, Marketplace] = {
    m.domain: m for m in (
        Marketplace("amazon.in", "Amazon India", "IN", "INR", "en-IN", "en-IN,en;q=0.9", "Asia/Kolkata"),
        Marketplace("amazon.com", "Amazon US", "US", "USD", "en-US", "en-US,en;q=0.9", "America/New_York"),
        Marketplace("amazon.co.uk", "Amazon UK", "GB", "GBP", "en-GB", "en-GB,en;q=0.9", "Europe/London"),
        Marketplace("amazon.ca", "Amazon Canada", "CA", "CAD", "en-CA", "en-CA,en;q=0.9", "America/Toronto"),
        Marketplace("amazon.com.au", "Amazon Australia", "AU", "AUD", "en-AU", "en-AU,en;q=0.9", "Australia/Sydney"),
    )
}


def resolve_marketplace(value: str | None) -> Marketplace:
    """Map the request's search.marketplace ('amazon.in', 'www.amazon.in',
    'https://www.amazon.in/') to a supported marketplace; raise ValueError otherwise."""
    domain = re.sub(r"^(https?://)?(www\.)?", "", (value or "").strip().lower()).split("/")[0]
    try:
        return MARKETPLACES[domain]
    except KeyError:
        raise ValueError(
            f"unsupported Amazon marketplace {value!r}; use one of {sorted(MARKETPLACES)}"
        )


class ReviewSort(str, Enum):
    recent = "recent"
    helpful = "helpful"


class AmazonOptions(BaseModel):
    include_sponsored: bool = False
    fulfilled_by_amazon_only: bool = False
    review_sort: ReviewSort = ReviewSort.recent
    verified_reviews_only: bool = False
    # Collect every review Amazon makes reachable (default), instead of only
    # ``review_pages_per_product`` batches of 10 from one listing.
    collect_all_reviews: bool = True

    model_config = {"extra": "forbid"}


def config_schema() -> PlatformConfigSchema:
    fields = [
        ConfigField(
            name="include_sponsored",
            label="Include sponsored listings",
            type=FieldType.boolean,
            default=False,
            description="Include sponsored/ad listings from search results.",
        ),
        ConfigField(
            name="fulfilled_by_amazon_only",
            label="Fulfilled by Amazon only",
            type=FieldType.boolean,
            default=False,
            description="Keep only products shipped/fulfilled by Amazon. Products whose "
                        "fulfilment cannot be determined are excluded.",
        ),
        ConfigField(
            name="review_sort",
            label="Review order",
            type=FieldType.enum,
            default=ReviewSort.recent.value,
            description="Order in which review pages are collected.",
            options=[
                FieldOption(value=ReviewSort.recent.value, label="Most recent"),
                FieldOption(value=ReviewSort.helpful.value, label="Top reviews"),
            ],
        ),
        ConfigField(
            name="verified_reviews_only",
            label="Verified purchase reviews only",
            type=FieldType.boolean,
            default=False,
            description="Collect only reviews marked 'Verified Purchase'.",
        ),
        ConfigField(
            name="collect_all_reviews",
            label="Collect all reviews",
            type=FieldType.boolean,
            default=True,
            description="Collect every review Amazon makes reachable for each product (review "
                        "listings are split by star rating, sort, media/format and keyword, since "
                        "Amazon shows at most 100 reviews per listing). When off, only "
                        "review_pages_per_product pages of 10 reviews are collected. "
                        "review_pages_per_product = 0 disables review pages either way.",
        ),
    ]
    return PlatformConfigSchema(platform="amazon", fields=fields)
