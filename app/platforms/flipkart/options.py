from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from app.schemas.config_schema import (
    ConfigField,
    FieldOption,
    FieldType,
    PlatformConfigSchema,
)


class ReviewSort(str, Enum):
    helpful = "helpful"
    recent = "recent"
    positive = "positive"
    negative = "negative"


class FlipkartOptions(BaseModel):
    review_sort: ReviewSort = ReviewSort.recent
    verified_purchase_only: bool = False
    include_sponsored: bool = False

    model_config = {"extra": "forbid"}


def config_schema() -> PlatformConfigSchema:
    fields = [
        ConfigField(
            name="review_sort",
            label="Review sort order",
            type=FieldType.enum,
            default=ReviewSort.recent.value,
            description="Order in which reviews are collected.",
            options=[
                FieldOption(value=ReviewSort.helpful.value, label="Most Helpful"),
                FieldOption(value=ReviewSort.recent.value, label="Most Recent"),
                FieldOption(value=ReviewSort.positive.value, label="Positive First"),
                FieldOption(value=ReviewSort.negative.value, label="Negative First"),
            ],
        ),
        ConfigField(
            name="verified_purchase_only",
            label="Verified purchases only",
            type=FieldType.boolean,
            default=False,
            description="Keep only reviews marked as verified purchase / certified buyer.",
        ),
        ConfigField(
            name="include_sponsored",
            label="Include sponsored listings",
            type=FieldType.boolean,
            default=False,
            description="Include sponsored/ad product listings in search results.",
        ),
    ]
    return PlatformConfigSchema(platform="flipkart", fields=fields)
