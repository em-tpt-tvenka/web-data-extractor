"""Amazon follows the Flipkart architecture: a Flipkart-shaped product model
from parsing/engine, and exactly one product_listing record per product with
reviews embedded. Synthetic HTML only (no network)."""

from __future__ import annotations

import asyncio

from app.platforms.amazon import parsing
from app.platforms.amazon.adapter import AmazonAdapter
from app.platforms.amazon.engine import AmazonEngine, AmazonEngineConfig
from app.platforms.amazon.mapper import _ProductContext, build_records
from app.platforms.amazon.options import AmazonOptions, resolve_marketplace
from app.platforms.base import ScrapeResult
from app.schemas.scraping import ScrapeJobRequest

ASIN = "B0TESTASIN"

PRODUCT_HTML = """
<html><body>
<div id="wayfinding-breadcrumbs_feature_div"><ul>
  <li><a>Computers &amp; Accessories</a></li><li><a>Laptops</a></li><li><a>Traditional Laptops</a></li>
</ul></div>
<span id="productTitle"> Lenovo IdeaPad Slim 5 (16GB RAM/512GB SSD) Laptop, Luna Grey </span>
<a id="bylineInfo">Visit the Lenovo Store</a>
<span id="acrPopover" title="4.2 out of 5 stars"></span>
<span id="acrCustomerReviewText">1,000 ratings</span>
<table id="histogramTable">
  <tr aria-label="70 percent of reviews have 5 stars"></tr>
  <tr aria-label="20 percent of reviews have 4 stars"></tr>
  <tr aria-label="5 percent of reviews have 3 stars"></tr>
  <tr aria-label="2 percent of reviews have 2 stars"></tr>
  <tr aria-label="3 percent of reviews have 1 stars"></tr>
</table>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="priceToPay"><span class="a-offscreen">₹58,990.00</span></span>
  <span class="basisPrice"><span class="a-offscreen">₹79,990.00</span></span>
  <span class="savingsPercentage">-26%</span>
</div>
<div id="availability"> In stock </div>
<div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">
  <a id="sellerProfileTriggerId" href="/gp/help/seller/at-a-glance.html?seller=A14CZOWI0VEHLG">Darshita Etel</a>
</span></div>
<div id="fulfillerInfoFeature_feature_div"><span class="offer-display-feature-text-message">Amazon</span></div>
<span data-csa-c-delivery-time="Tuesday, 25 November" data-csa-c-delivery-price="FREE"></span>
<div id="vsxoffers_feature_div">
  <div class="a-carousel-card"><h6>Bank Offer</h6><span class="a-truncate-full">Upto ₹1,500 off on HDFC cards</span></div>
  <div class="a-carousel-card"><h6>No Cost EMI</h6><span class="a-truncate-full">Save interest on EMI</span></div>
  <div class="a-carousel-card"><h6>Cashback</h6><span class="a-truncate-full">5% back with Amazon Pay</span></div>
</div>
<div id="iconfarmv2_feature_div">
  <span class="a-size-small">10 days Replacement</span>
  <span class="a-size-small">1 Year Manufacturer Warranty</span>
  <span class="a-size-small">Pay on Delivery</span>
</div>
<div id="feature-bullets"><ul>
  <li><span class="a-list-item">AMD Ryzen AI 5 330 processor</span></li>
  <li><span class="a-list-item">16GB LPDDR5x RAM, 512GB SSD</span></li>
</ul></div>
<div id="productDescription">A thin and light 14 inch laptop for work and study with all-day battery life.</div>
<table id="productDetails_techSpec_section_1">
  <tr><th>Brand</th><td>Lenovo</td></tr>
  <tr><th>Item model number</th><td>83HX00DCIN</td></tr>
  <tr><th>Colour</th><td>Luna Grey</td></tr>
  <tr><th>Manufacturer</th><td>Lenovo, Lenovo</td></tr>
  <tr><th>Country of Origin</th><td>China</td></tr>
</table>
<div id="productDetails_expanderTables_depthLeftSections">
  <div class="a-expander-container"><a class="a-expander-header"><span>Display</span></a>
    <div class="a-expander-content"><table><tr><th>Screen Size</th><td>14 Inches</td></tr></table></div>
  </div>
</div>
<table id="productDetails_detailBullets_sections1">
  <tr><th>ASIN</th><td>B0TESTASIN</td></tr>
  <tr><th>Customer Reviews</th><td>4.2 out of 5 stars</td></tr>
  <tr><th>Generic Name</th><td>Laptop</td></tr>
</table>
<div data-hook="review" id="R1PRODUCTPG">
  <span class="a-profile-name">Asha</span>
  <i data-hook="review-star-rating"><span class="a-icon-alt">5.0 out of 5 stars</span></i>
  <a data-hook="review-title"><span>Great laptop</span></a>
  <span data-hook="review-date">Reviewed in India on 5 March 2025</span>
  <span data-hook="avp-badge">Verified Purchase</span>
  <span data-hook="review-body"><span>Fast and light.</span></span>
  <span data-hook="helpful-vote-statement">12 people found this helpful</span>
</div>
<script>
var data = {'colorImages': { 'initial': [{"hiRes":"https://m.media-amazon.com/images/I/a1.jpg","thumb":"https://m.media-amazon.com/images/I/a1._SS40_.jpg"},{"hiRes":"https://m.media-amazon.com/images/I/a2.jpg","thumb":null}]}};
var vids = {"videos": [{"url": "https://m.media-amazon.com/v/demo.mp4"}]};
var twister = {"dimensionsDisplay":["Size","Colour"],"dimensionValuesDisplayData":{"B0TESTASIN":["16GB","Luna Grey"],"B0OTHERAS1":["32GB","Luna Grey"]}};
</script>
</body></html>
"""

REVIEW_PAGE_HTML = """
<html><body>
<div data-hook="cr-filter-info-review-rating-count">1,000 total ratings, 120 with reviews</div>
<div data-hook="review" id="R1PRODUCTPG">
  <span class="a-profile-name">Asha</span>
  <i data-hook="review-star-rating"><span class="a-icon-alt">5.0 out of 5 stars</span></i>
  <a data-hook="review-title"><span>Great laptop</span></a>
  <span data-hook="review-date">Reviewed in India on 5 March 2025</span>
  <span data-hook="avp-badge">Verified Purchase</span>
  <span data-hook="review-body"><span>Fast and light. Battery easily lasts a full working day.</span></span>
</div>
<div data-hook="review" id="R2REVIEWPG">
  <span class="a-profile-name">Ravi</span>
  <i data-hook="review-star-rating"><span class="a-icon-alt">2.0 out of 5 stars</span></i>
  <a data-hook="review-title"><span>Heats up</span></a>
  <span data-hook="review-date">Reviewed in India on 1 January 2020</span>
  <span data-hook="review-body"><span>Gets hot under load.</span></span>
</div>
<ul class="a-pagination"><li class="a-last a-disabled"><a>Next</a></li></ul>
</body></html>
"""

FLIPKART_REVIEW_KEYS = ["reviewer_name", "rating", "title", "review_text", "verified_purchase",
                        "posted_date", "posted_relative", "helpful_count"]


def _request(**opts) -> ScrapeJobRequest:
    return ScrapeJobRequest(platform="amazon",
                            search={"product": "laptop", "marketplace": "amazon.in"}, **opts)


def _engine() -> AmazonEngine:
    return AmazonEngine(AmazonEngineConfig(marketplace=resolve_marketplace("amazon.in"),
                                           search_text="laptop", review_pages=2))


def _model(engine: AmazonEngine | None = None) -> dict:
    """Run the real parsing + engine review step with the review page stubbed."""
    engine = engine or _engine()
    parsed = parsing.parse_product_page(PRODUCT_HTML, ASIN, "https://www.amazon.in", "INR")
    data = {"url": f"https://www.amazon.in/dp/{ASIN}", "product_id": ASIN, "is_sponsored": False,
            "scraped_at": "2026-11-20T00:00:00+00:00", "marketplace": "amazon.in", **parsed}
    review_page = parsing.parse_review_page(REVIEW_PAGE_HTML)

    async def fake_pages(page, asin):
        return {"reviews": review_page["reviews"], "pages_checked": 1,
                "total_reviews": review_page["total_reviews"], "login_required": False,
                "stop_reason": "last review page (1)"}

    engine._collect_review_pages = fake_pages
    data["review_summary"], data["reviews"] = asyncio.run(engine._scrape_reviews(None, data))
    return data


# ── parsing: Flipkart-shaped product model ───────────────────────────────────

def test_product_model_has_flipkart_shape():
    m = parsing.parse_product_page(PRODUCT_HTML, ASIN, "https://www.amazon.in", "INR")
    for key in ("product_id", "product_name", "url", "brand", "sku", "description", "price", "ratings",
                "offers", "delivery", "seller", "warranty_and_policies", "features", "specifications",
                "specifications_flat", "manufacturer", "images", "videos", "variants", "reviews"):
        assert key in m, key
    assert m["product_name"].startswith("Lenovo IdeaPad")
    assert m["brand"] == "Lenovo"
    assert m["price"]["selling_price"] == 58990.0 and m["price"]["mrp"] == 79990.0
    assert m["features"] == ["AMD Ryzen AI 5 330 processor", "16GB LPDDR5x RAM, 512GB SSD"]


def test_specifications_are_sectioned_like_flipkart():
    m = parsing.parse_product_page(PRODUCT_HTML, ASIN, "https://www.amazon.in", "INR")
    specs = m["specifications"]
    assert specs["Technical Details"]["Item model number"] == "83HX00DCIN"
    assert specs["Display"]["Screen Size"] == "14 Inches"  # expander header becomes the section
    assert "Customer Reviews" not in specs.get("Additional Information", {})
    assert m["specifications_flat"]["Display | Screen Size"] == "14 Inches"
    assert m["manufacturer"] == {"manufactured_by": "Lenovo", "country_of_origin": "China",
                                 "generic_name": "Laptop", "name": "Lenovo"}


def test_ratings_breakdown_counts_and_percent():
    r = parsing.parse_product_page(PRODUCT_HTML, ASIN, "https://www.amazon.in", "INR")["ratings"]
    assert r["average_rating"] == 4.2 and r["total_ratings"] == 1000
    assert r["breakdown"] == {"1_star": 30, "2_star": 20, "3_star": 50, "4_star": 200, "5_star": 700}
    assert r["breakdown_percent"]["5_star"] == 70


def test_media_variants_offers():
    m = parsing.parse_product_page(PRODUCT_HTML, ASIN, "https://www.amazon.in", "INR")
    assert m["images"] == ["https://m.media-amazon.com/images/I/a1.jpg", "https://m.media-amazon.com/images/I/a2.jpg"]
    assert m["videos"] == ["https://m.media-amazon.com/v/demo.mp4"]
    assert [o["option"] for o in m["variants"]["Size"]] == ["16GB", "32GB"]
    assert m["variants"]["Size"][1]["product_id"] == "B0OTHERAS1"
    assert m["variants"]["Colour"] == [{"option": "Luna Grey", "url": "https://www.amazon.in/dp/B0TESTASIN",
                                        "product_id": "B0TESTASIN"}]
    offers = m["offers"]
    assert offers["bank_offers"] == ["Bank Offer | Upto ₹1,500 off on HDFC cards"]
    assert offers["emi_offers"] == ["No Cost EMI | Save interest on EMI"]
    assert offers["additional_benefits"] == ["Cashback | 5% back with Amazon Pay"]
    assert offers["no_cost_emi_mentioned"] is True


def test_review_keys_match_flipkart():
    r = parsing.parse_reviews(parsing.soup_of(PRODUCT_HTML))[0]
    assert list(r)[:8] == FLIPKART_REVIEW_KEYS
    assert r["posted_date"] == "2025-03-05" and r["posted_relative"] == "5 March 2025"
    assert r["helpful_count"] == 12 and r["review_id"] == "R1PRODUCTPG"


# ── engine review step: pagination merge + duplicate handling ───────────────

def test_engine_review_step_merges_and_dedupes():
    m = _model()
    ids = [r["review_id"] for r in m["reviews"]]
    assert ids == ["R1PRODUCTPG", "R2REVIEWPG"]  # same review from both pages kept once
    merged = m["reviews"][0]
    assert merged["review_text"].endswith("full working day.")  # longer review-page text wins
    assert merged["helpful_count"] == 12  # filled from the product-page copy
    s = m["review_summary"]
    assert s["total_reviews"] == 120 and s["pages_scraped"] == 1
    assert s["rating_breakdown"]["5_star"] == 700 and s["sort_used"] == "recent"


def test_reviews_without_ids_are_deduplicated():
    a = {"review_id": None, "reviewer_name": "X", "rating": 4.0, "title": "t",
         "review_text": "same", "posted_date": "2025-01-01"}
    assert len(parsing.merge_reviews([a], [dict(a)])) == 1


# ── mapper: one listing per product, reviews embedded ───────────────────────

WIDE_WINDOW = {"date_range": {"type": "custom", "start_date": "2019-01-01", "end_date": "2026-12-31"}}


def test_one_listing_record_per_product_with_embedded_reviews():
    records = build_records(_model(), _request(scraping_options=WIDE_WINDOW), "amz_job_test")
    assert len(records) == 1
    assert len(records[0].reviews) == 2
    rec = records[0]
    assert rec.record_type == "product_listing"
    assert rec.source.source_item_id == ASIN and rec.source.country_code == "IN"
    assert rec.metrics.total_ratings == 1000 and rec.metrics.total_reviews == 120
    assert rec.metrics.rating_distribution["5_star"] == 700
    assert [list(r)[:8] for r in rec.reviews] == [FLIPKART_REVIEW_KEYS] * len(rec.reviews)
    assert rec.review_summary["reviews_collected"] == len(rec.reviews)


def test_review_date_window_is_applied():
    req = _request(scraping_options={"date_range": {"type": "custom", "start_date": "2025-01-01",
                                                     "end_date": "2025-12-31"}})
    rec = build_records(_model(), req, "amz_job_test")[0]
    assert [r["review_id"] for r in rec.reviews] == ["R1PRODUCTPG"]  # 2020 review dropped


def test_retailer_carries_amazon_and_seller():
    ret = build_records(_model(), _request(), "amz_job_test")[0].entities.retailer
    assert ret.retailer_id == "RETAILER_AMAZON" and ret.retailer_name == "Amazon"
    assert ret.seller_name == "Darshita Etel" and ret.seller_id == "A14CZOWI0VEHLG"
    assert ret.fulfilled_by_amazon is True and ret.marketplace == "amazon.in"


def test_commercial_taxonomy_attributes():
    model = _model()
    ctx = _ProductContext(model, _request())
    comm, tax, attr = ctx.commercial(), ctx.taxonomy(_request()), ctx.product_attributes()
    assert comm.price.listed_price.amount == 79990.0 and comm.price.selling_price.amount == 58990.0
    assert comm.price.discount_amount == 21000.0 and comm.price.price_type == "promotional"
    assert comm.price_range.minimum == 58990.0 and comm.price_range.maximum == 79990.0
    assert comm.delivery.delivery_type == "free_delivery" and comm.delivery.delivery_charge.amount == 0
    assert comm.warranty.duration_months == 12 and comm.warranty.warranty_type == "manufacturer"
    assert comm.payment_options.cash_on_delivery is True and comm.payment_options.bank_offer_available is True
    assert comm.offers_detail["bank_offers"]
    assert tax.industry == "Electronics" and tax.category == "Computers & Accessories"
    assert tax.subcategory == "Laptops" and tax.product_type == "Traditional Laptops"
    assert attr.model_number == "83HX00DCIN" and attr.color == "Luna Grey"
    assert attr.highlights and attr.manufacturer["name"] == "Lenovo"
    assert attr.specifications["Available Variants"]["Size"] == "16GB, 32GB"


def test_taxonomy_fallback_without_breadcrumbs():
    model = _model()
    model["breadcrumbs"] = []
    tax = _ProductContext(model, _request()).taxonomy(_request())
    assert tax.mapping_method == "search_derived" and tax.mapping_status == "mapped"
    # The page's own "Generic Name" beats the raw search text.
    assert tax.industry == "Electronics" and tax.category == "Laptop" and tax.product_type == "Laptop"


def test_missing_values_stay_null():
    model = {"product_id": ASIN, "product_name": "Bare item", "url": "u", "marketplace": "amazon.in"}
    rec = build_records(model, _request(), "amz_job_test")[0]
    assert rec.commercial.price.selling_price is None and rec.metrics.rating is None
    assert rec.reviews == [] and rec.review_summary is None and rec.entities.retailer.seller_id is None


# ── adapter filters on the flat model ───────────────────────────────────────

def test_adapter_filters_on_flat_model():
    adapter, model = AmazonAdapter(), _model()
    opts = AmazonOptions()
    assert adapter._passes_filters(model, _request(), opts)
    assert not adapter._passes_filters(model, _request(filters={"max_price": 50000}), opts)
    # Keywords are screened on the search title by the engine; the full product
    # title (which lists "16GB RAM/512GB SSD") must not drop the product.
    assert adapter._passes_filters(model, _request(filters={"exclude_keywords": ["RAM", "SSD"]}), opts)
    assert adapter._passes_filters(model, _request(), AmazonOptions(fulfilled_by_amazon_only=True))
    records = adapter.to_records(ScrapeResult(raw_products=[model, _model()]), _request(), "amz_job_test")
    assert len(records) == 2 and {r.record_type for r in records} == {"product_listing"}
