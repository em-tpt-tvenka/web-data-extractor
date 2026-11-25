from __future__ import annotations

from app.reporting.constants import Sentiment
from app.reporting.nlp.aspect_extractor import extract_aspects, extract_complaints
from app.reporting.nlp.sentiment_classifier import classify


# ── sentiment from rating ────────────────────────────────────────────────────

def test_rating_drives_sentiment():
    assert classify(5, "whatever") == Sentiment.positive.value
    assert classify(4, "") == Sentiment.positive.value
    assert classify(1, "") == Sentiment.negative.value
    assert classify(2, "") == Sentiment.negative.value
    assert classify(3, "") == Sentiment.neutral.value


def test_three_star_broken_by_text():
    assert classify(3, "great and excellent love it") == Sentiment.positive.value
    assert classify(3, "terrible waste broken") == Sentiment.negative.value


def test_strong_rating_with_opposite_text_is_mixed():
    # 5 stars but clearly negative language -> mixed
    assert classify(5, "defective broken useless") == Sentiment.mixed.value


def test_no_rating_uses_lexicon():
    assert classify(None, "excellent and great") == Sentiment.positive.value
    assert classify(None, "terrible and broken") == Sentiment.negative.value
    assert classify(None, "the item arrived tuesday") == Sentiment.unclassified.value


def test_negation_flips_polarity():
    # "not good" should not count as positive
    assert classify(None, "not good") != Sentiment.positive.value


# ── aspects & complaints ─────────────────────────────────────────────────────

def test_aspect_extraction_maps_to_taxonomy():
    aspects = extract_aspects("the camera is great but the battery drains", "mixed")
    themes = {a["theme"] for a in aspects}
    assert "Camera" in themes
    assert "Battery" in themes
    cats = {a["category"] for a in aspects}
    assert "Product" in cats


def test_complaints_only_on_negative_reviews():
    text = "installation was delayed and the battery drains"
    assert extract_complaints(text, Sentiment.negative.value)  # non-empty
    # same text, positive sentiment -> no complaints counted
    assert extract_complaints(text, Sentiment.positive.value) == []


def test_complaint_categories_detected():
    found = set(extract_complaints("delivery was late and warranty claim rejected",
                                   Sentiment.negative.value))
    assert "delivery" in found
    assert "warranty" in found
