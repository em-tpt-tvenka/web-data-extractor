"""Amazon adapter/mapper contract, and guards that Alibaba is unaffected."""

import asyncio
import copy

import pytest
from pydantic import ValidationError

from app.core.exceptions import RequestValidationError
from app.platforms import registry
from app.platforms.amazon import parsing
from app.platforms.amazon.adapter import AmazonAdapter
from app.platforms.amazon.engine import AmazonEngineConfig, build_review_url, build_search_url
from app.platforms.amazon.options import MARKETPLACES, resolve_marketplace
from app.platforms.base import ScrapeResult
from app.processing import normalizer
from app.schemas.scraping import ScrapeJobRequest

from tests.adapters.test_amazon_parsing import PRODUCT_HTML


def _request(**over):
    body = {"platform": "amazon", "search": {"product": "refrigerator", "marketplace": "amazon.in"},
            "scraping_options": {"date_range": {"type": "last_2_years"}}}
    body.update(over)
    return ScrapeJobRequest.model_validate(body)


def _model():
    detail = parsing.parse_product_page(PRODUCT_HTML, "B0TEST0001", "https://www.amazon.in", "INR")
    return {
        "product_details": {"product_id": "B0TEST0001", "product_name": detail["title"],
                            "product_detail_url": "https://www.amazon.in/dp/B0TEST0001",
                            "scraped_at": "2026-11-16T10:00:00+00:00"},
        "marketplace": "amazon.in", "is_sponsored": False, "search_card": {},
        "detail": detail,
        "review_collection": {"pages_checked": 0, "total_reviews": None, "login_required": True},
    }


# ── registry / options ───────────────────────────────────────────────────────

def test_both_platforms_registered():
    assert {"alibaba", "amazon"} <= set(registry.supported_platforms())
    assert type(registry.get_adapter("amazon")).__name__ == "AmazonAdapter"


def test_amazon_options_reject_unknown_keys():
    with pytest.raises(ValidationError):
        AmazonAdapter().validate_options({"verified_supplier_only": True})
    assert AmazonAdapter().validate_options({}).include_sponsored is False


@pytest.mark.parametrize("value", ["amazon.in", "www.amazon.in", "https://www.amazon.in/", "AMAZON.COM"])
def test_marketplace_normalization(value):
    assert resolve_marketplace(value).domain in MARKETPLACES


def test_validate_request_rejects_bad_marketplace_and_currency():
    a = AmazonAdapter()
    with pytest.raises(ValueError):
        a.validate_request(_request(search={"product": "x", "marketplace": "amazon.de"}))
    with pytest.raises(ValueError):
        a.validate_request(_request(filters={"currency": "USD"}))
    a.validate_request(_request(filters={"currency": "inr"}))


def test_job_manager_rejects_before_queueing():
    from app.orchestrator.job_manager import JobManager

    req = _request(search={"product": "x", "marketplace": "amazon.fr"})
    with pytest.raises(RequestValidationError):
        asyncio.run(JobManager().create_job(req))


# ── URL building ─────────────────────────────────────────────────────────────

def test_search_and_review_urls():
    cfg = AmazonEngineConfig(marketplace=MARKETPLACES["amazon.in"], search_text="hp laptop",
                             sort_by="price_asc", min_price=20000, max_price=50000)
    assert build_search_url(cfg, 2) == (
        "https://www.amazon.in/s?k=hp+laptop&s=price-asc-rank&rh=p_36%3A2000000-5000000&page=2")
    cfg.verified_reviews_only = True
    assert "reviewerType=avp_only_reviews" in build_review_url(cfg, "B0TEST0001", 3)
    assert "pageNumber=3" in build_review_url(cfg, "B0TEST0001", 3)


# ── mapping ──────────────────────────────────────────────────────────────────

def test_review_records_map_to_unified_schema():
    req = _request()
    (rec,) = AmazonAdapter().to_records(ScrapeResult(raw_products=[_model()]), req, "amz_job_x")
    normalizer.finalize([rec])
    assert rec.id.startswith("amz_mnt_")
    assert rec.record_type == "social_mention"
    assert rec.source.platform == "amazon" and rec.source.source_type == "review"
    assert rec.source.source_item_id == "R2SMQQ19B272PG"
    assert rec.source.parent_item_id == "B0TEST0001"
    assert rec.source.published_at == "2026-10-15" and rec.source.country_code == "IN"
    assert rec.content.text == "Cools fast. Quiet." and rec.content.title == "Good fridge"
    assert rec.metrics.rating.value == 4.0 and rec.metrics.helpful_votes == 14
    assert rec.metrics.review_count is None  # aggregates are not copied onto a review
    assert rec.author.is_verified_purchase is True
    assert rec.entities.brands[0].brand_name == "Samsung"
    assert rec.entities.retailer.retailer_id == "A14CZOWI0VEHLG"
    price = rec.commercial.price
    assert (price.selling_price.amount, price.original_price.amount, price.selling_price.currency) == \
        (25490.0, 36990.0, "INR")
    assert rec.commercial.delivery.delivery_type == "free"
    assert rec.commercial.warranty.duration_months == 12
    assert rec.product_attributes.capacity.unit == "litre"
    assert rec.taxonomy.category == "Home & Kitchen"
    assert rec.taxonomy.mapping_method == "source_breadcrumb"
    assert rec.quality.status == "accepted"


def test_reviews_outside_window_fall_back_to_listing():
    req = _request(scraping_options={"date_range": {"type": "custom", "start_date": "2020-01-01",
                                                    "end_date": "2020-12-31"}})
    (rec,) = AmazonAdapter().to_records(ScrapeResult(raw_products=[_model()]), req, "amz_job_x")
    assert rec.record_type == "product_listing"
    assert rec.source.source_item_id == "B0TEST0001"
    assert rec.metrics.rating.value == 4.3 and rec.metrics.review_count == 1234
    assert [m.media_type for m in rec.content.media] == ["image"]


def test_post_filters():
    a, m = AmazonAdapter(), _model()
    opts = a.validate_options({"fulfilled_by_amazon_only": True})
    assert a._passes_filters(m, _request(filters={"max_price": 30000}), opts)
    assert not a._passes_filters(m, _request(filters={"max_price": 20000}), opts)
    unknown = copy.deepcopy(m)
    unknown["detail"]["seller"]["fulfilled_by_amazon"] = None
    assert not a._passes_filters(unknown, _request(), opts)


# ── Alibaba is unaffected ────────────────────────────────────────────────────

def test_alibaba_contract_unchanged():
    a = registry.get_adapter("alibaba")
    assert [f.name for f in a.config_schema().fields] == [
        "verified_supplier_only", "trade_assurance_only", "min_order_quantity",
        "max_order_quantity", "supplier_country", "supplier_type", "include_sponsored"]
    with pytest.raises(ValidationError):
        a.validate_options({"include_sponsored": True, "review_sort": "recent"})
    # The new request hook is a no-op for Alibaba.
    a.validate_request(ScrapeJobRequest.model_validate(
        {"platform": "alibaba", "search": {"product": "bottle", "marketplace": "alibaba.com"}}))


# ── engine resilience ────────────────────────────────────────────────────────

class _ClosedPage:
    """Stands in for a Playwright page whose browser was closed."""

    def __init__(self):
        self.calls = 0

    def is_closed(self):
        return self.calls > 0

    async def goto(self, *a, **kw):
        self.calls += 1
        raise RuntimeError("Page.goto: Target page, context or browser has been closed")


def test_closed_browser_is_not_retried():
    from app.platforms.amazon.engine import AmazonEngine, BrowserClosedError

    cfg = AmazonEngineConfig(marketplace=MARKETPLACES["amazon.in"], search_text="x",
                             min_delay_seconds=0, max_delay_seconds=0, max_retries=3)
    page = _ClosedPage()
    with pytest.raises(BrowserClosedError):
        asyncio.run(AmazonEngine(cfg)._goto(page, "https://www.amazon.in/dp/B0TEST0001"))
    assert page.calls == 1
