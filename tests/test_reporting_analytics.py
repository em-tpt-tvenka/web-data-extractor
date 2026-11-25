from __future__ import annotations

import datetime

from app.reporting.analytics import (
    brand_analytics,
    complaint_analytics,
    kpi_calculator,
    risk_detection,
    trend_analytics,
)
from app.reporting.nlp.aspect_extractor import extract_aspects, extract_complaints
from app.reporting.nlp.sentiment_classifier import classify
from app.reporting.utils.report_helpers import (
    month_key,
    net_sentiment,
    parse_review_date,
    quarter_key,
    safe_pct,
)


def mk(platform="amazon", brand="Samsung", cat="smartphone", rating=5,
       text="great", d="2026-07-10"):
    sent = classify(rating, text)
    return {
        "review_id": f"{platform}:{brand}:{rating}:{text[:6]}:{d}", "source_record_id": "r",
        "platform": platform, "product_id": f"{brand}-1", "brand": brand, "category": cat,
        "product_name": f"{brand} {cat}", "rating": float(rating) if rating is not None else None,
        "title": None, "text": text, "date": parse_review_date(d), "country_code": "IN",
        "verified_purchase": True, "helpful_votes": 1, "language": "en", "sentiment": sent,
        "aspects": extract_aspects(text, sent), "complaints": extract_complaints(text, sent),
    }


# ── helpers ──────────────────────────────────────────────────────────────────

def test_safe_pct_and_net():
    assert safe_pct(1, 0) == 0.0
    assert safe_pct(1, 2) == 50.0
    assert net_sentiment(6, 2, 10) == 40.0
    assert net_sentiment(0, 0, 0) == 0.0


def test_date_parsing():
    assert parse_review_date("2026-07-15T10:00:00Z") == datetime.date(2026, 7, 15)
    assert parse_review_date("2026-08") == datetime.date(2026, 8, 1)
    assert parse_review_date("2 months ago") is None
    assert parse_review_date(None) is None


def test_period_keys():
    d = datetime.date(2026, 8, 15)
    assert month_key(d) == "2026-08"
    assert quarter_key(d) == "2026-Q3"


# ── coverage / sentiment ─────────────────────────────────────────────────────

def test_sentiment_summary_denominator_excludes_unclassified():
    reviews = [mk(rating=5), mk(rating=1, text="terrible"),
               mk(rating=None, text="arrived on a truck")]  # unclassified
    ss = kpi_calculator.sentiment_summary(reviews)
    assert ss.classified == 2
    assert ss.unclassified == 1
    assert ss.positive == 1 and ss.negative == 1
    assert ss.net_sentiment == 0.0


def test_coverage_counts():
    reviews = [mk(brand="Samsung"), mk(brand="Apple"), mk(brand="Samsung")]
    cov = kpi_calculator.data_coverage(reviews, product_count=2)
    assert cov.total_reviews == 3
    assert cov.brands == 2
    assert cov.total_products == 2


# ── brand / SoV ──────────────────────────────────────────────────────────────

def test_brand_share_of_voice_sums_to_100():
    reviews = [mk(brand="Samsung")] * 3 + [mk(brand="Apple")] * 1
    metrics, index = brand_analytics.brand_metrics(reviews)
    sov = {m.brand: m.share_of_voice_pct for m in metrics}
    assert sov["Samsung"] == 75.0
    assert sov["Apple"] == 25.0
    assert index["brand.Samsung.mentions"] == 3


def test_brandless_reviews_excluded_from_sov():
    reviews = [mk(brand="Samsung")] * 2
    reviews.append(mk(brand=None))  # type: ignore[arg-type]
    reviews[-1]["brand"] = None
    metrics, _ = brand_analytics.brand_metrics(reviews)
    # Only Samsung has a brand -> 100% SoV over branded reviews.
    assert metrics[0].share_of_voice_pct == 100.0


# ── complaints ───────────────────────────────────────────────────────────────

def test_complaint_summary_counts():
    reviews = [mk(rating=1, text="delivery late"), mk(rating=2, text="battery drains"),
               mk(rating=5, text="delivery was great")]  # positive -> no complaint
    metrics, index = complaint_analytics.complaint_summary(reviews)
    cats = {m.category: m.count for m in metrics}
    assert cats.get("delivery") == 1
    assert cats.get("battery") == 1
    assert index["complaint.total"] == 2


# ── trends: growth guard ─────────────────────────────────────────────────────

def test_trend_growth_none_without_prior_period():
    reviews = [mk(d="2026-07-10"), mk(d="2026-07-12")]  # single month
    summary, index = trend_analytics.monthly_trends(reviews)
    assert summary.mom_growth_pct is None  # only one month
    assert len(summary.points) == 1


def test_trend_mom_growth_computed():
    reviews = [mk(d="2026-07-10")] * 2 + [mk(d="2026-08-10")] * 4
    summary, _ = trend_analytics.monthly_trends(reviews)
    assert summary.mom_growth_pct == 100.0  # 2 -> 4


def test_no_dated_reviews_handled():
    reviews = [mk(d=None)]
    reviews[0]["date"] = None
    summary, _ = trend_analytics.monthly_trends(reviews)
    assert summary.points == []
    assert "unavailable" in (summary.note or "")


# ── risk: needs a prior quarter to escalate ──────────────────────────────────

def test_risk_cannot_escalate_without_prior_quarter():
    reviews = [mk(rating=1, text="battery drains terrible", d="2026-09-10")] * 20
    items, index = risk_detection.risk_summary(
        reviews, growth_threshold=20.0, negative_share_threshold=55.0, min_volume=10)
    assert index["risk.has_prior_quarter"] is False
    assert all(i.zone != "escalate" for i in items)  # no QoQ growth available


def test_risk_escalates_with_growth_and_negativity():
    prev = [mk(rating=1, text="battery drains", d="2026-06-10")] * 5
    curr = [mk(rating=1, text="battery drains", d="2026-09-10")] * 20
    items, index = risk_detection.risk_summary(
        prev + curr, growth_threshold=20.0, negative_share_threshold=55.0, min_volume=10)
    battery = next(i for i in items if i.theme == "Battery")
    assert battery.growth_qoq_pct is not None and battery.growth_qoq_pct >= 20.0
    assert battery.zone == "escalate"


def test_empty_dataset_is_safe():
    assert kpi_calculator.sentiment_summary([]).classified == 0
    assert brand_analytics.brand_metrics([])[0] == []
    assert complaint_analytics.complaint_summary([])[0] == []
