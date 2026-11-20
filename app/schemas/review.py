from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class MediaItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    media_type: str
    url: str

class ContentBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    text: str | None = None
    language: str | None = None
    original_language: str | None = None
    content_type: str | None = None
    media: list[MediaItem] | dict[str, Any] | list[Any] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

class RatingMetric(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: float | None = None
    scale_min: int = 1
    scale_max: int = 5

class EngagementMetric(BaseModel):
    model_config = ConfigDict(extra="allow")

    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    reactions: int | None = None

class MetricsBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    rating: RatingMetric | None = None
    engagement: EngagementMetric = Field(default_factory=EngagementMetric)
    helpful_votes: int | None = None
    review_count: int | None = None
    author_followers: int | None = None
    reach: int | None = None
    overall_rating: float | None = None
    total_ratings: int | None = None
    total_reviews: int | None = None
    rating_distribution: dict[str, Any] | None = None

class AuthorBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_author_id_hash: str | None = None
    display_name: str | None = None
    author_type: str | None = None
    is_verified_purchase: bool | None = None
    profile_url: str | None = None
    follower_count: int | None = None
    seller_rating: float | None = None
    badge: str | None = None
    location: str | None = None

class LocationBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    location_precision: str | None = None
