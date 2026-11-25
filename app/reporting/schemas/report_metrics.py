"""Typed metric blocks produced by the analytics engine and stored inside a
``report_summaries`` document. Every block permits ``extra`` so analytics can
attach platform-specific detail without a schema change."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_cfg = ConfigDict(extra="allow")


class DataCoverage(BaseModel):
    model_config = _cfg
    total_products: int = 0
    total_reviews: int = 0
    unique_reviews: int = 0
    duplicate_reviews: int = 0
    platforms: int = 0
    brands: int = 0
    categories: int = 0
    reviews_with_rating: int = 0
    reviews_with_text: int = 0
    reviews_with_date: int = 0
    date_min: str | None = None
    date_max: str | None = None


class DataQuality(BaseModel):
    model_config = _cfg
    classified_reviews: int = 0
    unclassified_reviews: int = 0
    missing_rating_pct: float = 0.0
    missing_text_pct: float = 0.0
    missing_date_pct: float = 0.0
    missing_brand_pct: float = 0.0
    missing_category_pct: float = 0.0
    notes: list[str] = Field(default_factory=list)


class SentimentSummary(BaseModel):
    model_config = _cfg
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    mixed: int = 0
    unclassified: int = 0
    classified: int = 0
    positive_pct: float = 0.0
    negative_pct: float = 0.0
    neutral_pct: float = 0.0
    mixed_pct: float = 0.0
    net_sentiment: float = 0.0
    average_rating: float | None = None


class BrandMetric(BaseModel):
    model_config = _cfg
    brand: str
    mentions: int = 0
    share_of_voice_pct: float = 0.0
    net_sentiment: float = 0.0
    positive_pct: float = 0.0
    negative_pct: float = 0.0
    average_rating: float | None = None
    top_themes: list[dict] = Field(default_factory=list)


class TopicMetric(BaseModel):
    model_config = _cfg
    theme: str
    category: str
    mentions: int = 0
    share_pct: float = 0.0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    mixed: int = 0
    net_sentiment: float = 0.0
    dominant_sentiment: str = "neutral"


class ComplaintMetric(BaseModel):
    model_config = _cfg
    category: str
    count: int = 0
    share_of_complaints_pct: float = 0.0


class RiskItem(BaseModel):
    model_config = _cfg
    theme: str
    category: str
    volume: int = 0
    negative_share_pct: float = 0.0
    growth_qoq_pct: float | None = None
    zone: str = "watch"  # watch | monitor | escalate
    rule_triggered: bool = False
    rationale: str = ""


class PlatformMetric(BaseModel):
    model_config = _cfg
    platform: str
    reviews: int = 0
    net_sentiment: float = 0.0
    positive_pct: float = 0.0
    negative_pct: float = 0.0
    average_rating: float | None = None


class ProductMetric(BaseModel):
    model_config = _cfg
    product_id: str
    platform: str
    product_name: str | None = None
    brand: str | None = None
    reviews: int = 0
    net_sentiment: float = 0.0
    average_rating: float | None = None


class TrendPoint(BaseModel):
    model_config = _cfg
    period: str          # e.g. "2026-07" or "2026-Q3"
    reviews: int = 0
    net_sentiment: float = 0.0
    average_rating: float | None = None


class TrendSummary(BaseModel):
    model_config = _cfg
    granularity: str = "monthly"
    points: list[TrendPoint] = Field(default_factory=list)
    mom_growth_pct: float | None = None
    qoq_growth_pct: float | None = None
    note: str | None = None


class CompetitiveInsight(BaseModel):
    model_config = _cfg
    dimension: str       # e.g. "net_sentiment", "share_of_voice"
    leader: str | None = None
    laggard: str | None = None
    detail: str = ""
