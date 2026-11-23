"""Map raw Amazon engine models to the unified record schema.

Same contract as the Flipkart mapper (the reference implementation): every
product yields exactly ONE ``product_listing`` record that carries

* the product (``entities.products`` + ``product_attributes``),
* the retailer / seller (``entities.retailer``),
* the customer reviews (``reviews``) and their aggregate (``review_summary``,
  ``metrics``),

so product details are never duplicated once per review. Amazon-specific
behaviour kept from the previous mapper: native review ids (``R...``), the
request's review date window, the marketplace's own country, and "never
fabricate" (fields the page does not show stay null).
"""

from __future__ import annotations

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.amazon.options import resolve_marketplace
from app.platforms.amazon.parsing import to_number
from app.platforms.categories import RECORD_TYPE_LISTING
from app.platforms.common import (
    empty_campaign,
    is_blank,
    media_items,
    now_iso,
    parse_date,
    placeholder_tenant,
    sha_hash,
    warranty_months,
)
from app.processing.language import detect_language
from app.schemas.record import (
    AuthorBlock,
    AvailabilityBlock,
    BrandEntity,
    Capacity,
    CommercialBlock,
    ContentBlock,
    DeduplicationBlock,
    DeliveryBlock,
    EntitiesBlock,
    LifecycleBlock,
    MetricsBlock,
    MoneyAmount,
    PaymentOptionsBlock,
    PriceBlock,
    PriceRangeBlock,
    ProductAttributesBlock,
    ProductEntity,
    PromotionBlock,
    ProvenanceBlock,
    QualityBlock,
    RatingMetric,
    RetailerEntity,
    SocialMentionRecord,
    SourceBlock,
    TaxonomyBlock,
    WarrantyBlock,
)
from app.schemas.scraping import ScrapeJobRequest

PLATFORM = "amazon"
CONNECTOR_NAME = "amazon_connector"
CONNECTOR_VERSION = "1.1.0"
NORMALIZER_VERSION = "1.1.0"
RETAILER_ID = "RETAILER_AMAZON"

# Keys of every embedded review: Flipkart's eight first, then Amazon-only ones.
_REVIEW_KEYS = ("reviewer_name", "rating", "title", "review_text", "verified_purchase",
                "posted_date", "posted_relative", "helpful_count")
_AMAZON_REVIEW_KEYS = ("review_id", "reviewer_location", "country_code", "variant", "images", "videos")

_INDUSTRY_WORDS = (
    ("Electronics", ("mobile", "phone", "laptop", "computer", "tv", "television", "headphone", "audio",
                     "camera", "electronic", "tablet", "monitor", "wearable", "smartwatch")),
    ("Home Appliances", ("refrigerator", "washing machine", "air conditioner", "microwave", "appliance",
                         "vacuum", "water purifier")),
    ("Fashion", ("shirt", "dress", "clothing", "shoe", "footwear", "jeans", "fashion", "apparel",
                 "jewellery", "watch")),
    ("Beauty & Personal Care", ("beauty", "cosmetic", "perfume", "skincare", "skin care", "hair", "lipstick",
                                "makeup", "personal care", "grooming", "shampoo", "lotion", "serum")),
    ("Home & Kitchen", ("furniture", "kitchen", "home", "bed", "decor")),
)

_log = get_logger("amazon.mapper")


def _money(amount, currency) -> MoneyAmount | None:
    return MoneyAmount(amount=float(amount), currency=currency) if amount is not None else None


def _infer_industry(text: str) -> str:
    t = (text or "").lower()
    for industry, words in _INDUSTRY_WORDS:
        if any(w in t for w in words):
            return industry
    return "Consumer Goods"


def _source_item_id(review: dict, asin: str) -> str:
    """Amazon's own review id (R...) when present; otherwise a clearly-marked
    content-derived fallback over the FULL review text. Never the product id."""
    if not is_blank(review.get("review_id")):
        return str(review["review_id"])
    return "gen:" + sha_hash(asin, review.get("reviewer_name") or "", review.get("posted_date") or "",
                             review.get("review_text") or "")


class _ProductContext:
    """Normalized, reusable view of one raw (Flipkart-shaped) product model."""

    def __init__(self, model: dict, request: ScrapeJobRequest):
        self.model = model
        self.flat = model.get("specifications_flat", {}) or {}
        self.product_id = model.get("product_id") or (model.get("product_details") or {}).get("product_id")
        self.subject = model.get("product_name")
        self.product_url = model.get("url")
        self.scraped_at = model.get("scraped_at") or now_iso()
        self.brand = None if is_blank(model.get("brand")) else model["brand"]
        self.sku = model.get("sku") or self.product_id
        self.description = model.get("description")

        self.marketplace = model.get("marketplace") or request.search.marketplace
        try:
            mp = resolve_marketplace(self.marketplace)
            self.country_code, self.marketplace_name, default_currency = mp.country_code, mp.name, mp.currency
        except ValueError:
            self.country_code, self.marketplace_name, default_currency = None, None, None

        self.price = model.get("price", {}) or {}
        self.currency = self.price.get("currency") or default_currency
        self.ratings = model.get("ratings", {}) or {}
        self.offers = model.get("offers", {}) or {}
        self.policies = model.get("warranty_and_policies", {}) or {}
        self.delivery = model.get("delivery", {}) or {}
        self.seller = model.get("seller", {}) or {}
        self.promotion = model.get("promotion", {}) or {}
        self.payment = model.get("payment", {}) or {}
        self.breadcrumbs = model.get("breadcrumbs") or []

        self.model_number = None if is_blank(model.get("model_number")) else model["model_number"]
        self.color = None if is_blank(model.get("color")) else model["color"]
        self.capacity = model.get("capacity")
        self.energy_rating = model.get("energy_rating")
        self.highlights = model.get("features", []) or []
        self.variants = model.get("variants") or []
        self.manufacturer = model.get("manufacturer") or None

        warranty_text = model.get("warranty_text") or next(iter(self.policies.get("warranty") or []), None)
        self.warranty_months = warranty_months(warranty_text)
        self.warranty_type = "manufacturer" if "manufactur" in (warranty_text or "").lower() else None

    def entities(self) -> EntitiesBlock:
        brands = [BrandEntity(brand_name=self.brand)] if self.brand else []
        products = [ProductEntity(
            product_id=self.product_id, product_name=self.subject,
            sku=self.sku, model_number=self.model_number, product_url=self.product_url,
        )]
        # Retailer is the platform (as Flipkart); the marketplace seller that
        # owns the buy box is kept alongside it.
        retailer = RetailerEntity(
            retailer_id=RETAILER_ID,
            retailer_name="Amazon",
            marketplace=self.marketplace,
            marketplace_name=self.marketplace_name,
            seller_id=self.seller.get("seller_id"),
            seller_name=self.seller.get("name"),
            seller_rating=self.seller.get("rating"),
            ships_from=self.seller.get("ships_from"),
            fulfilled_by_amazon=self.seller.get("fulfilled_by_amazon"),
        )
        return EntitiesBlock(brands=brands, products=products, retailer=retailer)

    def product_attributes(self) -> ProductAttributesBlock:
        specs = dict(self.model.get("specifications", {}) or {})
        if isinstance(self.variants, dict) and self.variants:
            summary = {
                name: ", ".join(o.get("option", str(o)) if isinstance(o, dict) else str(o) for o in opts)
                for name, opts in self.variants.items() if isinstance(opts, list) and opts
            }
            if summary:
                specs["Available Variants"] = summary
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=Capacity(**self.capacity) if self.capacity else None,
            color=self.color,
            energy_rating=self.energy_rating,
            highlights=self.highlights,
            variants=self.variants,
            manufacturer=self.manufacturer,
            specifications=specs,
        )

    def commercial(self) -> CommercialBlock:
        price = self.price
        selling = price.get("selling_price")
        original = price.get("mrp")
        special = price.get("special_deal_price")
        final_selling = special or selling
        listed = original or final_selling

        disc_amt = price.get("discount_amount")
        disc_pct = to_number(price.get("discount_percent"))
        if disc_amt is None and original and final_selling and original > final_selling:
            disc_amt = round(original - final_selling, 2)
        if disc_pct is None and disc_amt and original:
            disc_pct = round(disc_amt / original * 100, 2)

        offers = self.offers
        has_bank = bool(offers.get("bank_offers"))
        deal_or_coupon = bool(self.promotion.get("is_promotional"))
        is_promo = bool(disc_amt or disc_pct or has_bank or deal_or_coupon)
        promo_type = self.promotion.get("promotion_type") or (
            "discount" if (disc_amt or disc_pct) else ("bank_offer" if has_bank else None))

        min_p = final_selling
        max_p = original if (original and min_p and original >= min_p) else final_selling

        cost = self.delivery.get("delivery_cost")
        delivery_type = "free_delivery" if cost == 0 else ("paid_delivery" if cost else None)
        avail = self.model.get("availability") or {}

        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(listed, self.currency),
                selling_price=_money(final_selling, self.currency),
                original_price=_money(original, self.currency),
                discount_amount=disc_amt,
                discount_percentage=disc_pct,
                price_type="promotional" if is_promo else "regular",
                price_observed_at=self.scraped_at,
                lowest_price_with_offers=_money(special, self.currency) if special else None,
                offer_discount_amount=price.get("offer_discount_amount"),
                extra_discount_amount=price.get("extra_discount_amount"),
            ),
            price_range=PriceRangeBlock(
                minimum=min_p, maximum=max_p,
                currency=self.currency if min_p is not None else None,
            ),
            promotion=PromotionBlock(
                is_promotional=is_promo,
                promotion_type=promo_type,
                promotion_name=self.promotion.get("promotion_name"),
                coupon_available=self.promotion.get("coupon_available"),
            ),
            # Amazon signals are True only on explicit page evidence, else None.
            payment_options=PaymentOptionsBlock(
                emi_available=self.payment.get("emi_available"),
                no_cost_emi=self.payment.get("no_cost_emi"),
                bank_offer_available=self.payment.get("bank_offer_available") or (True if has_bank else None),
                exchange_offer_available=self.payment.get("exchange_offer_available"),
                cash_on_delivery=self.payment.get("cash_on_delivery"),
            ),
            availability=AvailabilityBlock(status=avail.get("status"),
                                           stock_quantity=avail.get("stock_quantity")),
            delivery=DeliveryBlock(
                delivery_charge=_money(cost, self.currency),
                estimated_delivery_date=self.delivery.get("estimate"),
                delivery_type=delivery_type,
            ),
            warranty=WarrantyBlock(duration_months=self.warranty_months, warranty_type=self.warranty_type)
            if (self.warranty_months or self.warranty_type) else None,
            offers_detail=offers if offers else None,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        crumbs = [c for c in self.breadcrumbs if c and c.lower() != "home"]
        if crumbs:
            cat = crumbs[0]
            subcat = crumbs[1] if len(crumbs) > 1 else None
            ptype = crumbs[-1] if len(crumbs) > 2 else (subcat or cat)
            return TaxonomyBlock(
                industry=_infer_industry(" ".join(crumbs)),
                category=cat, subcategory=subcat, product_type=ptype,
                mapping_method="source_breadcrumb", mapping_status="mapped",
            )

        # No breadcrumb: fall back to the search context (Amazon's own category
        # names are not guessed from keywords).
        gen_name = (self.manufacturer or {}).get("generic_name")
        search_cat = request.search.category
        search_query = request.search.product or self.subject
        cat = search_cat or gen_name or search_query
        has = bool(cat)
        return TaxonomyBlock(
            industry=_infer_industry(" ".join(filter(None, [gen_name, search_cat, search_query]))) if has else None,
            category=cat if has else None,
            subcategory=gen_name if (gen_name and gen_name != cat) else None,
            product_type=(gen_name or search_query) if has else None,
            mapping_method="search_derived" if has else "unmapped",
            mapping_status="mapped" if has else "unmapped",
        )

    def reviews(self, request: ScrapeJobRequest) -> list[dict]:
        """Embedded reviews in Flipkart's shape, restricted to the request's
        date window (undated reviews are kept) and de-duplicated by review id."""
        window = resolve_window(request.scraping_options.date_range)
        out, seen = [], set()
        for r in self.model.get("reviews") or []:
            try:
                if not in_window(parse_date(r.get("posted_date")), window):
                    continue
                rid = _source_item_id(r, self.product_id or "")
                if rid in seen:
                    continue
                seen.add(rid)
                entry = {k: r.get(k) for k in _REVIEW_KEYS}
                entry["review_text"] = (entry["review_text"] or "").strip() or None
                entry["title"] = (entry["title"] or "").strip() or None
                entry["verified_purchase"] = bool(entry["verified_purchase"])
                entry.update({k: r.get(k) for k in _AMAZON_REVIEW_KEYS})
                entry["review_id"] = rid
                entry["language"] = detect_language(entry["review_text"] or entry["title"])
                entry["reviewer_id_hash"] = sha_hash(r["reviewer_name"]) if r.get("reviewer_name") else None
                out.append(entry)
            except Exception as exc:  # one malformed review must not drop the product
                _log.warning("event=review_map_failed product=%s error=%s", self.product_id, exc)
        return out

    def review_summary(self, kept_reviews: int) -> dict | None:
        summary = dict(self.model.get("review_summary") or {})
        if not summary:
            return None
        summary["reviews_collected"] = kept_reviews
        return summary


def _provenance(run_id: str) -> ProvenanceBlock:
    return ProvenanceBlock(
        collection_run_id=run_id,
        connector_name=CONNECTOR_NAME,
        connector_version=CONNECTOR_VERSION,
        normalizer_version=NORMALIZER_VERSION,
        ingestion_timestamp=now_iso(),
    )


def _listing_record(ctx: _ProductContext, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    content_hash = identifiers.content_hash(ctx.product_id, ctx.subject or "")
    record_id = identifiers.record_id(PLATFORM)
    rating = ctx.ratings.get("average_rating")
    total_ratings = ctx.ratings.get("total_ratings")
    total_reviews = ctx.ratings.get("total_reviews")
    breakdown = ctx.ratings.get("breakdown") or {}
    body = ctx.description or (" | ".join(ctx.highlights) if ctx.highlights else None) or ctx.subject
    reviews = ctx.reviews(request)

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_listing",
            source_type="product",
            # A listing is product-level, so its source item id is the ASIN.
            source_item_id=ctx.product_id,
            source_url=ctx.product_url,
            collected_at=ctx.scraped_at,
            country_code=ctx.country_code,
            marketplace=ctx.marketplace,
        ),
        content=ContentBlock(
            title=ctx.subject,
            text=body,
            language=detect_language(body),
            content_type="product_listing",
            media=media_items(ctx.model.get("images"), ctx.model.get("videos")),
            keywords=ctx.highlights,
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=rating) if rating is not None else None,
            review_count=total_reviews,
            overall_rating=rating,
            total_ratings=total_ratings,
            total_reviews=total_reviews,
            rating_distribution=breakdown,
        ),
        author=AuthorBlock(),
        location=None,
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=_provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
        review_summary=ctx.review_summary(len(reviews)),
        reviews=reviews,
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    # One master product-listing record per product (as Flipkart): offers, clean
    # sectioned specifications with variants, review summary and the customer
    # reviews embedded, so product details are not repeated once per review.
    return [_listing_record(ctx, request, run_id)]
