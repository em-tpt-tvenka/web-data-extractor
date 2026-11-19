from __future__ import annotations

import pytest

from app.platforms.samsung import mapper, parsing
from app.processing.validator import validate_record
from app.schemas.scraping import ScrapeJobRequest

WIDE = {"type": "custom", "start_date": "2000-01-01", "end_date": "2100-01-01"}


def _request(category=None, date_range=None):
    return ScrapeJobRequest(
        platform="samsung",
        search={"product": "galaxy buds", "marketplace": "samsung.com", "category": category},
        scraping_options={"date_range": date_range or WIDE},
    )


def _model(**over):
    base = {
        "url": "https://www.samsung.com/in/audio-sound/galaxy-buds/galaxy-buds-core-black-sm-r410nzkainu/",
        "product_id": "SM-R410NZKAINU",
        "model_number": "SM-R410NZKAINU",
        "product_name": "Galaxy Buds Core Black",
        "brand": "Samsung",
        "category": "audio-devices",
        "color": "Black",
        "source_country": "IN",
        "breadcrumbs": ["Audio", "Galaxy Buds", "Galaxy Buds Core"],
        "warranty_text": "1 Year Manufacturer Warranty",
        "description": "Compact wireless earbuds.",
        "price": {"currency": "INR", "selling_price": 5000.0, "mrp": 6000.0, "availability": "in_stock"},
        "ratings": {"average_rating": 4.3, "total_reviews": 12},
        "specifications": {"General": {"Colour": "Black"}},
        "specifications_flat": {"General | Colour": "Black", "General | Warranty": "1 Year Manufacturer Warranty"},
        "images": ["https://images.samsung.com/x.jpg"],
        "videos": [],
        "introduced_date": "2024-03-01",
        "reviews": [],
        "scraped_at": "2026-11-19T00:00:00+00:00",
    }
    base.update(over)
    return base


def _review(**over):
    r = {
        "review_id": None, "rating": 5.0, "title": "Great",
        "review_text": "Loved the sound quality a lot", "reviewer_name": "Asha",
        "reviewer_country": "IN", "verified_purchase": True,
        "posted_date": "2025-06-10", "helpful_count": 3,
    }
    r.update(over)
    return r


def _dump(rec):
    return rec.model_dump(by_alias=True)


# ---------------- model number ----------------

@pytest.mark.parametrize("bad", ["Purchase Price", "Price", "₹10,000", "Available", "Black", "", None, True, "MRP"])
def test_invalid_model_numbers_rejected(bad):
    assert parsing._valid_model_number(bad) is None


@pytest.mark.parametrize("good,expected", [
    ("SM-R410NZKAINU", "SM-R410NZKAINU"),
    ("sm-s928bzkd", "sm-s928bzkd"),
    ("  SM-F956B  ", "SM-F956B"),
])
def test_valid_model_numbers_accepted(good, expected):
    assert parsing._valid_model_number(good) == expected


def test_model_number_from_digitaldata():
    html = "var x; digitalData.product.model_code = 'SM-R410NZKAINU'; more"
    assert parsing._model_number(html, "https://x/y/", [], {}) == "SM-R410NZKAINU"


def test_model_number_from_mpn():
    assert parsing._model_number("", "https://x/", [{"mpn": "SM-S928BZKD"}], {}) == "SM-S928BZKD"


def test_model_number_from_url():
    url = "https://www.samsung.com/in/audio/galaxy-buds-core-black-sm-r410nzkainu/"
    assert parsing._model_number("", url, [], {}) == "SM-R410NZKAINU"


def test_model_number_purchase_price_never_wins():
    flat = {"Pricing | Model": "Purchase Price"}
    assert parsing._model_number("", "https://x/", [], flat) is None


def test_model_number_entity_and_attrs_consistent():
    r = _dump(mapper.build_records(_model(), _request(), "job")[0])
    assert r["entities"]["products"][0]["model_number"] == r["product_attributes"]["model_number"] == "SM-R410NZKAINU"


def test_missing_model_number_is_none_not_fabricated():
    r = _dump(mapper.build_records(_model(model_number=None), _request(), "job")[0])
    assert r["product_attributes"]["model_number"] is None


# ---------------- colour ----------------

def test_colour_from_title():
    assert parsing._color_from_text("Galaxy Buds FE Graphite") == "Graphite"


def test_colour_spec_preferred_over_title():
    assert parsing._color({"A | Color": "Mystic White"}, "Galaxy Buds Black", "x") == "Mystic White"


def test_colour_from_url_when_no_spec_or_title():
    url = "https://www.samsung.com/in/x/galaxy-buds-core-graphite-sm-r400/"
    assert parsing._color({}, None, url) == "Graphite"


def test_colour_colour_and_color_keys():
    assert parsing._color({"A | Colour": "Navy"}, None, None) == "Navy"


def test_missing_colour_not_guessed():
    assert parsing._color({}, "Galaxy Buds Core", "https://x/galaxy-buds-core/") is None


# ---------------- country / marketplace ----------------

def test_source_country_india():
    assert parsing._source_country("https://www.samsung.com/in/audio/x-sm-r400/") == "IN"


def test_source_country_regional():
    assert parsing._source_country("https://www.samsung.com/us/audio/x/") == "US"


def test_country_not_from_reviewer_location():
    m = _model(source_country="IN", reviews=[_review(reviewer_country="DE")])
    r = _dump(mapper.build_records(m, _request(), "job")[0])
    assert r["source"]["country_code"] == "IN"       # marketplace country
    assert r["source"]["marketplace"] == "IN"
    assert r["location"]["country_code"] == "DE"      # reviewer location kept separate


# ---------------- taxonomy ----------------

def test_taxonomy_from_breadcrumbs():
    r = _dump(mapper.build_records(_model(), _request(), "job")[0])
    tax = r["taxonomy"]
    assert tax["category"] == "Audio" and tax["subcategory"] == "Galaxy Buds"
    assert tax["product_type"] == "Galaxy Buds Core" and tax["mapping_method"] == "source_breadcrumb"
    assert tax["mapping_status"] == "mapped"


def test_taxonomy_category_fallback():
    r = _dump(mapper.build_records(_model(breadcrumbs=[]), _request(category="Audio"), "job")[0])
    assert r["taxonomy"]["mapping_method"] == "search_derived"
    assert r["taxonomy"]["mapping_status"] == "mapped"


def test_taxonomy_unmapped_without_category():
    r = _dump(mapper.build_records(_model(breadcrumbs=[], category=None), _request(category=None), "job")[0])
    assert r["taxonomy"]["mapping_status"] == "unmapped"


# ---------------- commercial ----------------

def test_commercial_price_discount_and_currency():
    c = _dump(mapper.build_records(_model(), _request(), "job")[0])["commercial"]
    assert c["price"]["selling_price"] == {"amount": 5000.0, "currency": "INR"}
    assert c["price"]["original_price"]["amount"] == 6000.0
    assert c["price"]["discount_percentage"] == pytest.approx(16.67, abs=0.01)
    assert c["promotion"]["is_promotional"] is True


def test_commercial_warranty_extracted():
    c = _dump(mapper.build_records(_model(), _request(), "job")[0])["commercial"]
    assert c["warranty"] == {"duration_months": 12, "warranty_type": "manufacturer"}


def test_commercial_warranty_none_when_absent():
    c = _dump(mapper.build_records(_model(warranty_text=None), _request(), "job")[0])["commercial"]
    assert c["warranty"] is None


def test_commercial_payment_delivery_null_when_unavailable():
    c = _dump(mapper.build_records(_model(), _request(), "job")[0])["commercial"]
    assert c["payment_options"] is None and c["delivery"] is None


# ---------------- review ids / dedup ----------------

def test_native_review_id_used():
    m = _model(reviews=[_review(review_id="BV-123")])
    r = _dump(mapper.build_records(m, _request(), "job")[0])
    assert r["source"]["source_item_id"] == "BV-123"


def test_gen_review_id_deterministic_and_uses_full_text():
    long_a = "x" * 90 + "AAA"
    long_b = "x" * 90 + "BBB"
    a1 = mapper._source_item_id(_review(review_id=None, review_text=long_a), "PID")
    a2 = mapper._source_item_id(_review(review_id=None, review_text=long_a), "PID")
    b = mapper._source_item_id(_review(review_id=None, review_text=long_b), "PID")
    assert a1 == a2 and a1.startswith("gen:")
    assert a1 != b  # differ only past char 80 -> full text used


# ---------------- dates / record generation ----------------

def test_listing_when_no_reviews():
    recs = mapper.build_records(_model(reviews=[]), _request(), "job")
    assert len(recs) == 1 and recs[0].record_type == "product_listing"


def test_reviews_in_window_produce_review_records():
    m = _model(reviews=[_review(posted_date="2025-06-10"), _review(posted_date="2025-07-01", reviewer_name="Ravi")])
    recs = mapper.build_records(m, _request(date_range=WIDE), "job")
    assert len(recs) == 2 and all(r.record_type == "social_mention" for r in recs)


def test_reviews_outside_window_fall_back_to_listing():
    dr = {"type": "custom", "start_date": "2026-01-01", "end_date": "2026-12-31"}
    recs = mapper.build_records(_model(reviews=[_review(posted_date="2020-01-01")]), _request(date_range=dr), "job")
    assert len(recs) == 1 and recs[0].record_type == "product_listing"


def test_listing_published_at_not_launch_date():
    r = _dump(mapper.build_records(_model(introduced_date="2024-03-01"), _request(), "job")[0])
    assert r["source"]["published_at"] is None


def test_review_published_at_is_review_date():
    m = _model(reviews=[_review(posted_date="2025-06-10")])
    r = _dump(mapper.build_records(m, _request(date_range=WIDE), "job")[0])
    assert r["source"]["published_at"] == "2025-06-10"


# ---------------- schema validation ----------------

def test_records_pass_schema_validation():
    for m in (_model(), _model(reviews=[_review()])):
        for rec in mapper.build_records(m, _request(date_range=WIDE), "job"):
            v = validate_record(rec)
            assert v.quality.is_valid is True


def test_specs_commerce_rows_skipped():
    from bs4 import BeautifulSoup
    html = """
    <table><tr><th>Purchase Price</th><td>₹10,000</td></tr>
    <tr><th>Model Number</th><td>SM-R410</td></tr></table>
    """
    specs = parsing._specifications(BeautifulSoup(html, "html.parser"), [])
    flat = parsing.flatten_specs(specs)
    keys = [k.split(" | ")[-1] for k in flat]
    assert "Purchase Price" not in keys
    assert "Model Number" in keys
