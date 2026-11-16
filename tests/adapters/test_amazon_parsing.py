"""Amazon HTML parsers. Snippets mirror real amazon.in / amazon.com markup
(including the 2026 review-card layout) so these run offline."""

from app.platforms.amazon import parsing

BASE = "https://www.amazon.in"

SEARCH_HTML = """
<div data-component-type="s-search-result" data-asin="B0AAAAAAA1">
  <div data-cy="title-recipe"><h2><span>HP 15s Laptop 16GB 512GB SSD</span></h2></div>
  <span class="a-price"><span class="a-offscreen">₹52,990</span></span>
</div>
<div data-component-type="s-search-result" data-asin="B0AAAAAAA2" class="AdHolder">
  <h2><a><span>Laptop bag</span></a></h2>
  <span class="puis-sponsored-label-text">Sponsored</span>
</div>
<div data-component-type="s-search-result" data-asin="">not a product</div>
<a class="s-pagination-next" href="/s?k=hp+laptop&amp;page=2">Next</a>
"""

PRODUCT_HTML = """
<html><body>
<div id="wayfinding-breadcrumbs_feature_div"><ul>
  <li><a>Home &amp; Kitchen</a></li><li><a>Large Appliances</a></li><li><a>Refrigerators</a></li></ul></div>
<span id="productTitle"> Samsung 253 L, 3 Star, Convertible 5-in-1 Digital Inverter, Double Door Refrigerator </span>
<a id="bylineInfo">Visit the Samsung Store</a>
<span id="acrPopover" title="4.3 out of 5 stars"></span>
<span id="acrCustomerReviewText">1,234 ratings</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="savingsPercentage">-31%</span>
  <span class="a-price priceToPay"><span class="a-offscreen">₹25,490</span></span>
  <span class="basisPrice">M.R.P.: <span class="a-price a-text-price"><span class="a-offscreen">₹36,990</span></span></span>
</div>
<div id="dealBadge_feature_div">Limited time deal</div>
<div id="availability"><span>Only 3 left in stock.</span></div>
<div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">
  <a href="/gp/help/seller/at-a-glance.html?seller=A14CZOWI0VEHLG">Appario Retail</a></span></div>
<div id="fulfillerInfoFeature_feature_div"><span class="offer-display-feature-text-message">Amazon</span></div>
<span data-csa-c-delivery-time="Monday, 23 November" data-csa-c-delivery-price="FREE"></span>
<div id="vsxoffers_feature_div"><div class="a-carousel-card">
  <h6>No Cost EMI</h6><span class="a-truncate-full">Upto ₹1,100 EMI interest savings</span></div>
  <div class="a-carousel-card"><h6>Bank Offer</h6><span class="a-truncate-full">10% instant discount</span></div></div>
<div id="iconfarmv2_feature_div"><span class="icon-content">Pay on Delivery</span>
  <span class="icon-content">1 Year Warranty</span></div>
<div id="feature-bullets"><ul><li><span class="a-list-item">Frost free, 253 litres</span></li></ul></div>
<table id="productDetails_techSpec_section_1">
  <tr><th>Brand</th><td>Samsung</td></tr>
  <tr><th>Capacity</th><td>253 litres</td></tr>
  <tr><th>Energy Efficiency</th><td>3 Star</td></tr>
  <tr><th>Item model number</th><td>RT28C3733S8</td></tr>
  <tr><th>Colour</th><td>Elegant Inox</td></tr>
  <tr><th>Customer Reviews</th><td>4.3 out of 5</td></tr>
</table>
<div id="detailBullets_feature_div"><ul><li><span class="a-list-item">
  <span class="a-text-bold">Country of Origin :</span> India</span></li></ul></div>
<div id="histogramTable"><a aria-label="70 percent of reviews have 5 stars"></a>
  <a aria-label="5 percent of reviews have 1 star"></a></div>
<script>'colorImages': { 'initial': [{"hiRes":"https://m.media-amazon.com/images/I/a1.jpg","thumb":"https://m.media-amazon.com/images/I/a1._SS40_.jpg"}]}</script>
<div data-hook="review" id="R2SMQQ19B272PG">
  <span class="a-profile-name">Zulfiqar</span>
  <i data-hook="review-star-rating"><span class="a-icon-alt">4 out of 5 stars</span></i>
  <h5 data-hook="reviewTitle">Good fridge</h5>
  <span data-hook="review-date">Reviewed in India on 15 October 2026</span>
  <span data-hook="avp-badge">Verified Purchase</span>
  <div data-hook="reviewText">
    <div class="a-teaser-describedby-collapsed a-hidden">Brief content visible, double tap to read full content.</div>
    <div data-hook="reviewRichContentContainer"><p><span>Cools fast.</span></p><p><span>Quiet.</span></p></div>
    <div data-hook="reviewExpandButtonContainer"><span class="a-see-more-text">Read more</span></div>
  </div>
  <span data-hook="helpful-vote-statement">14 people found this helpful</span>
  <img class="_x_single-review-image-tile_3c" src="https://m.media-amazon.com/images/I/r1._SY500_.jpg">
</div>
</body></html>
"""


def test_search_page_cards_and_next():
    items, nxt = parsing.parse_search_page(SEARCH_HTML, BASE)
    assert [i["asin"] for i in items] == ["B0AAAAAAA1", "B0AAAAAAA2"]
    assert items[0]["title"] == "HP 15s Laptop 16GB 512GB SSD"
    assert items[0]["is_sponsored"] is False and items[1]["is_sponsored"] is True
    assert nxt == "https://www.amazon.in/s?k=hp+laptop&page=2"


def test_product_page_core_fields():
    d = parsing.parse_product_page(PRODUCT_HTML, "B0TEST0001", BASE, "INR")
    assert d["brand"] == "Samsung"
    assert d["model_number"] == "RT28C3733S8"
    assert d["color"] == "Elegant Inox"
    assert d["capacity"] == {"value": 253.0, "unit": "litre"}
    assert d["energy_rating"] == "3 Star"
    assert d["country_of_origin"] == "India"
    assert d["breadcrumbs"] == ["Home & Kitchen", "Large Appliances", "Refrigerators"]
    assert "Customer Reviews" not in d["specifications"]
    assert d["availability"] == {"status": "limited_stock", "stock_quantity": 3}
    assert d["media"]["images"] == ["https://m.media-amazon.com/images/I/a1.jpg"]


def test_product_page_pricing_and_commerce():
    d = parsing.parse_product_page(PRODUCT_HTML, "B0TEST0001", BASE, "INR")
    p = d["pricing"]
    assert (p["currency"], p["current_price"], p["original_price"]) == ("INR", 25490.0, 36990.0)
    assert p["discount_percentage"] == 31.0 and p["discount_amount"] == 11500.0
    assert d["seller"] == {"seller_name": "Appario Retail", "seller_id": "A14CZOWI0VEHLG",
                           "ships_from": "Amazon", "fulfilled_by_amazon": True}
    assert d["delivery"] == {"estimated_delivery": "Monday, 23 November", "delivery_cost": 0.0}
    assert d["promotion"]["is_promotional"] and d["promotion"]["promotion_type"] == "deal"
    pay = d["payment"]
    assert pay["no_cost_emi"] and pay["bank_offer_available"] and pay["cash_on_delivery"]
    assert pay["exchange_offer_available"] is None  # no evidence -> None, not False
    assert d["rating"] == {"overall_rating": 4.3, "total_ratings": 1234,
                           "distribution": {"5_star": 70, "1_star": 5}}


def test_review_card_layout_strips_accessibility_text():
    (r,) = parsing.parse_product_page(PRODUCT_HTML, "B0TEST0001", BASE, "INR")["reviews"]
    assert r["review_id"] == "R2SMQQ19B272PG"
    assert r["text"] == "Cools fast. Quiet."
    assert r["title"] == "Good fridge"
    assert (r["rating"], r["date"], r["country_code"]) == (4.0, "2026-10-15", "IN")
    assert r["verified_purchase"] is True and r["helpful_votes"] == 14
    assert r["images"] == ["https://m.media-amazon.com/images/I/r1.jpg"]


def test_not_a_product_page_raises():
    import pytest
    with pytest.raises(ValueError):
        parsing.parse_product_page("<html><body>nothing</body></html>", "B0TEST0001", BASE, "INR")


def test_original_price_not_higher_is_not_a_discount():
    html = """<div id="corePriceDisplay_desktop_feature_div">
      <span class="priceToPay"><span class="a-offscreen">$49.99</span></span>
      <span class="basisPrice"><span class="a-offscreen">$49.99</span></span></div>"""
    p = parsing.extract_pricing(parsing.soup_of(html), "USD")
    assert p["original_price"] is None and p["discount_percentage"] is None


def test_capacity_title_ignores_weight():
    assert parsing.parse_capacity(None, "HP Chromebook 14, 1.35kg") is None
    assert parsing.parse_capacity(None, "LG 1.5 Ton 5 Star Split AC") == {"value": 1.5, "unit": "ton"}
    assert parsing.parse_capacity("7 kg", None) == {"value": 7.0, "unit": "kg"}


def test_availability_states():
    assert parsing.parse_availability("Currently unavailable.")["status"] == "out_of_stock"
    assert parsing.parse_availability("In stock")["status"] == "in_stock"
    assert parsing.parse_availability(None)["status"] is None


def test_blocked_and_signin_detection():
    assert parsing.looks_blocked('<form action="/errors/validateCaptcha">')
    assert parsing.is_signin_page("", "https://www.amazon.in/ap/signin?openid")
    assert not parsing.is_signin_page("<html></html>", "https://www.amazon.in/dp/B0X")


def test_merge_reviews_prefers_complete_copy():
    short = {"review_id": "R1AAAAAAAA", "text": "Good", "rating": 5.0, "images": []}
    full = {"review_id": "R1AAAAAAAA", "text": "Good fridge, cools fast", "rating": None,
            "images": ["u"]}
    (m,) = parsing.merge_reviews([full], [short])
    assert m["text"] == "Good fridge, cools fast" and m["rating"] == 5.0 and m["images"] == ["u"]


def test_converted_price_keeps_displayed_currency_and_decimals():
    # amazon.com viewed from India: empty a-offscreen, ISO code, decimal span in whole.
    html = """<div id="corePriceDisplay_desktop_feature_div"><span class="a-price priceToPay">
      <span class="a-offscreen"> </span><span aria-hidden="true"><span class="a-price-symbol">INR</span>
      <span class="a-price-whole">2,804<span class="a-price-decimal">.</span></span>
      <span class="a-price-fraction">10</span></span></span></div>"""
    p = parsing.extract_pricing(parsing.soup_of(html), "USD")
    assert (p["current_price"], p["currency"]) == (2804.10, "INR")


def test_not_deliverable_and_deal_badge():
    assert parsing.parse_availability(
        "This item cannot be shipped to your selected delivery location.")["status"] == \
        "not_deliverable_to_location"
    html = """<div class="dealBadge"><span class="aok-hidden">NO_OF_HOURS hours</span>
      <span>Early Black Friday Deal</span></div>"""
    promo = parsing.extract_promotion(parsing.soup_of(html), [])
    assert promo["promotion_name"] == "Early Black Friday Deal" and promo["promotion_type"] == "deal"


def test_combined_shipper_seller_and_coupon_label():
    html = """<div id="merchantInfoFeature_feature_div"><span>Shipper / Seller</span>
      <span class="offer-display-feature-text-message">Amazon.com</span></div>
      <label id="couponText123">Apply ₹1000 coupon Terms</label>"""
    soup = parsing.soup_of(html)
    seller = parsing.extract_seller(soup)
    assert seller["ships_from"] == "Amazon.com" and seller["fulfilled_by_amazon"] is True
    assert parsing.extract_promotion(soup, [])["promotion_name"] == "Apply ₹1000 coupon"
