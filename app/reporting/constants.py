"""Enumerations and canonical names shared across the reporting engine."""

from __future__ import annotations

from enum import Enum


class ReportStatus(str, Enum):
    """Lifecycle of a report-generation job (mirrors the spec's status list)."""

    pending = "PENDING"
    data_processing = "DATA_PROCESSING"
    analytics_running = "ANALYTICS_RUNNING"
    summary_saved = "SUMMARY_SAVED"
    llm_generating = "LLM_GENERATING"
    validating = "VALIDATING"
    generating_files = "GENERATING_FILES"
    completed = "COMPLETED"
    failed = "FAILED"
    partially_completed = "PARTIALLY_COMPLETED"


class SummaryType(str, Enum):
    competitive_analysis = "competitive_analysis"
    brand_health = "brand_health"
    category_overview = "category_overview"
    voice_of_customer = "voice_of_customer"


class ReportType(str, Enum):
    competitive_analysis = "competitive_analysis"
    brand_health = "brand_health"
    category_overview = "category_overview"
    voice_of_customer = "voice_of_customer"


class Sentiment(str, Enum):
    positive = "positive"
    negative = "negative"
    neutral = "neutral"
    mixed = "mixed"
    unclassified = "unclassified"


class OutputFormat(str, Enum):
    pdf = "pdf"
    xlsx = "xlsx"


class ArtifactStatus(str, Enum):
    available = "available"
    missing = "missing"
    failed = "failed"


# Canonical report sections. Each maps to a prompt, an Excel sheet and PDF flow.
SECTION_EXECUTIVE_SUMMARY = "executive_summary"
SECTION_SCORECARD = "kpi_scorecard"
SECTION_BRAND_COMPARISON = "brand_comparison"
SECTION_SENTIMENT = "sentiment_analysis"
SECTION_CATEGORY = "category_analysis"
SECTION_TOPIC = "topic_analysis"
SECTION_COMPLAINT = "complaint_analysis"
SECTION_RISK = "risk_analysis"
SECTION_RECOMMENDATIONS = "recommendations"

ALL_SECTIONS = [
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_SCORECARD,
    SECTION_BRAND_COMPARISON,
    SECTION_SENTIMENT,
    SECTION_CATEGORY,
    SECTION_TOPIC,
    SECTION_COMPLAINT,
    SECTION_RISK,
    SECTION_RECOMMENDATIONS,
]

# Sections in the reference PDF that marketplace-review data cannot support.
# They are declared unavailable with the data source each one would require.
UNSUPPORTED_SECTIONS: dict[str, str] = {
    "social_channel_mix": "social listening feed (X, Instagram, YouTube, Facebook, Reddit, Forums)",
    "engagement_kpis": "social engagement metrics (likes, shares, views, reach)",
    "influencer_voice": "influencer / creator tracking feed",
    "media_news_coverage": "news & trade-media monitoring feed",
    "share_of_voice_social": "cross-channel social mention volume (reviews are not social mentions)",
}

# Design-system palette from the reference report's appendix (page 27).
PALETTE = {
    "coral": "#FC6A52",   # negative sentiment
    "orange": "#F48C39",
    "yellow": "#FBBA2D",  # motif only
    "green": "#7CBE3D",
    "blue": "#55ABD6",    # positive sentiment
    "purple": "#A854E1",
    "ink": "#1F2430",     # text
    "neutral": "#B8BEC9",  # neutral sentiment
    "white": "#FFFFFF",
}

SENTIMENT_COLORS = {
    Sentiment.positive.value: PALETTE["blue"],
    Sentiment.negative.value: PALETTE["coral"],
    Sentiment.neutral.value: PALETTE["neutral"],
    Sentiment.mixed.value: PALETTE["purple"],
}

# Brand-series colour rotation for multi-brand charts.
BRAND_SERIES_COLORS = [
    PALETTE["coral"], PALETTE["blue"], PALETTE["green"],
    PALETTE["purple"], PALETTE["orange"], PALETTE["ink"], PALETTE["yellow"],
]
