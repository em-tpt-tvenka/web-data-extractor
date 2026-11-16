from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from app.schemas.config_schema import ConfigField, FieldOption, FieldType, PlatformConfigSchema


class ReviewSort(str, Enum):
    newest = "submission-desc"
    helpful = "helpful"
    rating_high = "rating-desc"
    rating_low = "rating-asc"


class WalmartOptions(BaseModel):
    review_sort: ReviewSort = ReviewSort.newest

    model_config = {"extra": "forbid"}


def config_schema() -> PlatformConfigSchema:
    fields = [
        ConfigField(
            name="review_sort",
            label="Review sort order",
            type=FieldType.enum,
            default=ReviewSort.newest.value,
            description="Order in which reviews are collected.",
            options=[
                FieldOption(value=ReviewSort.newest.value, label="Newest first"),
                FieldOption(value=ReviewSort.helpful.value, label="Most helpful"),
                FieldOption(value=ReviewSort.rating_high.value, label="Highest rating"),
                FieldOption(value=ReviewSort.rating_low.value, label="Lowest rating"),
            ],
        ),
    ]
    return PlatformConfigSchema(platform="walmart", fields=fields)
