from __future__ import annotations

from pydantic import BaseModel, Field

class MediaItem(BaseModel):
    media_type: str
    url: str

class ContentBlock(BaseModel):
    title: str | None = None
    text: str | None = None
    language: str | None = None
    original_language: str | None = None
    content_type: str | None = None
    media: list[MediaItem] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

class RatingMetric(BaseModel):
    value: float | None = None
    scale_min: int = 1
    scale_max: int = 5

class EngagementMetric(BaseModel):
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    reactions: int | None = None

class MetricsBlock(BaseModel):
    rating: RatingMetric | None = None
    engagement: EngagementMetric = Field(default_factory=EngagementMetric)
    helpful_votes: int | None = None
    review_count: int | None = None
    author_followers: int | None = None
    reach: int | None = None

class AuthorBlock(BaseModel):
    source_author_id_hash: str | None = None
    display_name: str | None = None
    author_type: str | None = None
    is_verified_purchase: bool | None = None
    profile_url: str | None = None
    follower_count: int | None = None

class LocationBlock(BaseModel):
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    location_precision: str | None = None
