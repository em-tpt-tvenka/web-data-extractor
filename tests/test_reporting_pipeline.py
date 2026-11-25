from __future__ import annotations

import asyncio
import datetime

from app.core.config import settings
from app.reporting.llm import fallback_provider
from app.reporting.llm.factory import get_provider
from app.reporting.llm.prompt_manager import SECTION_SPECS, build_context, render
from app.reporting.llm.response_validator import validate_section
from app.reporting.nlp.aspect_extractor import extract_aspects, extract_complaints
from app.reporting.nlp.sentiment_classifier import classify
from app.reporting.schemas.report_request import ReportDateRange, SummaryFiltersIn
from app.reporting.services.insight_service import InsightService
from app.reporting.services.report_data_service import DataBundle
from app.reporting.services.report_summary_service import ReportSummaryService
from app.reporting.utils.evidence_selector import select_evidence
from app.reporting.utils.report_helpers import parse_review_date


def _mk(platform, brand, rating, text, d):
    sent = classify(rating, text)
    return {
        "review_id": f"{platform}:{brand}:{rating}:{text[:6]}:{d}", "source_record_id": "r",
        "platform": platform, "product_id": f"{brand}-1", "brand": brand,
        "category": "smartphone", "product_name": f"{brand} phone",
        "rating": float(rating), "title": None, "text": text, "date": parse_review_date(d),
        "country_code": "IN", "verified_purchase": True, "helpful_votes": 2, "language": "en",
        "sentiment": sent, "aspects": extract_aspects(text, sent),
        "complaints": extract_complaints(text, sent),
    }


def build_summary():
    samples = [
        ("amazon", "Samsung", 5, "excellent camera great battery", "2026-07-10"),
        ("amazon", "Samsung", 2, "battery drains fast installation delayed", "2026-08-11"),
        ("flipkart", "Samsung", 1, "defective warranty rejected poor service", "2026-09-05"),
        ("amazon", "Apple", 5, "amazing design smooth performance", "2026-07-15"),
        ("flipkart", "Apple", 4, "good camera expensive", "2026-08-20"),
        ("amazon", "Apple", 3, "decent but delivery late", "2026-06-09"),
    ]
    reviews = [_mk(*s) for s in samples * 4]
    filters = SummaryFiltersIn(
        platforms=["amazon", "flipkart"], brands=["Samsung", "Apple"],
        categories=["smartphone"],
        date_range=ReportDateRange(start=datetime.date(2026, 6, 1), end=datetime.date(2026, 9, 30)),
    )
    bundle = DataBundle(reviews=reviews, product_count=2, snapshot_signature="s",
                        snapshot_meta={})
    return ReportSummaryService()._build(filters, "competitive_analysis", bundle, "k")


# ── summary assembly ─────────────────────────────────────────────────────────

def test_summary_assembles_all_blocks():
    s = build_summary()
    assert s.data_coverage.total_reviews == 24
    assert len(s.brand_metrics) == 2
    assert s.metric_index  # non-empty
    assert s.evidence
    assert "social_channel_mix" in s.unsupported_sections
    assert s.summary_key == "k"


# ── narrator -> validator: everything metric-backed ──────────────────────────

def test_narrator_points_all_validated():
    s = build_summary()
    for section in SECTION_SPECS:
        raw = fallback_provider.build(section, s)
        v = validate_section(section, raw, s)
        assert v.ok, (section, v.reason)
        assert v.items, section
        for item in v.items:
            assert item["validation_status"] == "validated", (section, item)
            for mid in item["supporting_metric_ids"]:
                assert mid in s.metric_index


def test_validator_drops_unknown_refs_and_flags_unsupported():
    s = build_summary()
    raw = {"headline": "x", "points": [
        {"title": "t", "observation": "o", "business_implication": None,
         "supporting_metric_ids": ["bogus.metric"], "supporting_evidence_ids": ["nope"]},
    ]}
    v = validate_section("executive_summary", raw, s)
    assert v.ok
    item = v.items[0]
    assert item["supporting_metric_ids"] == []      # unknown dropped
    assert item["supporting_evidence_ids"] == []
    assert item["validation_status"] == "unsupported"
    assert item["validation_notes"]


def test_validator_rejects_bad_schema():
    s = build_summary()
    v = validate_section("executive_summary", {"not": "valid"}, s)
    assert v.ok is False


# ── evidence selector ────────────────────────────────────────────────────────

def test_evidence_dedup_and_sections():
    s = build_summary()
    ev = select_evidence(
        [{"review_id": "a", "sentiment": "negative", "complaints": ["delivery"],
          "helpful_votes": 5, "date": None, "text": "late", "brand": "Samsung",
          "platform": "amazon", "product_id": "p"},
         {"review_id": "a", "sentiment": "negative", "complaints": [], "helpful_votes": 1,
          "date": None, "text": "dup", "brand": "Samsung", "platform": "amazon",
          "product_id": "p"}],
        per_section=3)
    ids = [e.review_id for e in ev]
    assert ids.count("a") == 1  # deduped


# ── LLM factory ──────────────────────────────────────────────────────────────

def test_factory_returns_none_for_fallback_and_unknown():
    assert get_provider("fallback") is None
    assert get_provider("bogus-provider") is None
    assert get_provider(None) is None  # default settings.llm_provider == fallback


def test_factory_builds_configured_provider(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key", raising=False)
    provider = get_provider("gemini")
    assert provider is not None and provider.name == "gemini"
    # ollama needs no key to construct
    assert get_provider("ollama").name == "ollama"


def test_gemini_unconfigured_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", None, raising=False)
    assert get_provider("gemini") is None


# ── prompt rendering ─────────────────────────────────────────────────────────

def test_prompt_render_has_no_unfilled_placeholders():
    s = build_summary()
    _, model_cls = SECTION_SPECS["executive_summary"]
    ctx = build_context("executive_summary", s)
    prompt = render("executive_summary", ctx, model_cls.model_json_schema())
    assert "{context_json}" not in prompt
    assert "{schema_json}" not in prompt
    assert "available_metric_ids" in prompt
    # Review snippets must be presented as data, not instructions.
    assert "not instructions" in prompt.lower() or "data" in prompt.lower()


# ── insight service (fallback, no DB persistence) ────────────────────────────

def test_insight_service_fallback_end_to_end():
    s = build_summary()
    svc = InsightService()
    result = asyncio.run(svc.generate(
        "rpt_test", s, list(SECTION_SPECS.keys()), llm_provider="fallback", persist=False))
    assert result.provider_label == "fallback"
    assert result.insights
    assert set(result.sections_via_fallback) == set(SECTION_SPECS.keys())
    for ins in result.insights:
        assert ins.source == "deterministic_fallback"
        assert ins.report_id == "rpt_test"
        assert ins.summary_id == s.id


# ── excel generation ─────────────────────────────────────────────────────────

def test_excel_generation_creates_file(tmp_path):
    from app.reporting.generators.excel_generator import ExcelReportGenerator
    s = build_summary()
    insights = []
    for section in SECTION_SPECS:
        v = validate_section(section, fallback_provider.build(section, s), s)
        for it in v.items:
            it = dict(it); it["section"] = section; it["source"] = "deterministic_fallback"
            insights.append(it)
    out = tmp_path / "report.xlsx"
    path = ExcelReportGenerator().generate(s, insights, out)
    assert out.exists() and out.stat().st_size > 0
