from __future__ import annotations

from app.platforms.alibaba import mapper as ali
from app.platforms.amazon import mapper as amz
from app.platforms.common import country_code

LONG_A = "x" * 90 + "AAA"
LONG_B = "x" * 90 + "BBB"


# ---- Alibaba now uses the shared (richer) country map ----

def test_alibaba_uses_shared_country_map():
    for name, code in [("United Arab Emirates", "AE"), ("Singapore", "SG"),
                       ("Netherlands", "NL"), ("China", "CN")]:
        assert country_code(name) == code


# ---- review source ids: native preferred, gen uses FULL text ----

def test_alibaba_native_review_id_preferred():
    assert ali._source_item_id({"review_id": "R-9"}, "k", "r", "d", "t") == "R-9"
    assert ali._source_item_id({"id": "I-3"}, "k", "r", "d", "t") == "I-3"


def test_alibaba_gen_id_deterministic_full_text():
    a1 = ali._source_item_id({"id": ""}, "cr", "Li", "2025-01-01", LONG_A)
    a2 = ali._source_item_id({"id": ""}, "cr", "Li", "2025-01-01", LONG_A)
    b = ali._source_item_id({"id": ""}, "cr", "Li", "2025-01-01", LONG_B)
    assert a1 == a2 and a1.startswith("gen:")
    assert a1 != b  # differ only past char 80 -> full text used


def test_amazon_native_review_id_preferred():
    assert amz._source_item_id({"review_id": "R123"}, "ASIN") == "R123"


def test_amazon_gen_id_full_text():
    # Amazon reviews use Flipkart's review keys (review_text / posted_date).
    a = amz._source_item_id({"review_id": None, "reviewer_name": "Jo", "posted_date": "2025-01-01",
                             "review_text": LONG_A}, "ASIN")
    b = amz._source_item_id({"review_id": None, "reviewer_name": "Jo", "posted_date": "2025-01-01",
                             "review_text": LONG_B}, "ASIN")
    assert a != b and a.startswith("gen:")


# ---- Alibaba warranty via shared helper ----

def test_alibaba_warranty_years_months_none():
    assert ali._parse_warranty("2 Years Manufacturer Warranty").duration_months == 24
    assert ali._parse_warranty("18 months").duration_months == 18
    assert ali._parse_warranty("N/A") is None
    assert ali._parse_warranty(None) is None
    w = ali._parse_warranty("1 Year Manufacturer Warranty")
    assert w.warranty_type == "manufacturer"
