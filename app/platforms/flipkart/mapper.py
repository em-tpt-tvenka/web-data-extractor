from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.categories import RECORD_TYPE_LISTING, RECORD_TYPE_MENTION
from app.platforms.flipkart import parsing
from app.processing.language import detect_language
from app.schemas.record import (
    AuthorBlock,
    AvailabilityBlock,
    BrandEntity,
    Capacity,
    CampaignBlock,
    CommercialBlock,
    ContentBlock,
    DeduplicationBlock,
    DeliveryBlock,
    EntitiesBlock,
    LifecycleBlock,
    LocationBlock,
    MediaItem,
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

PLATFORM = "flipkart"
CONNECTOR_NAME = "flipkart_connector"
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"
_UNASSIGNED = "__unassigned__"

_log = get_logger("flipkart.mapper")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()


def _money(amount, currency) -> MoneyAmount | None:
    return MoneyAmount(amount=amount, currency=currency) if amount is not None else None


def _placeholder_tenant():
    from app.schemas.record import TenantBlock
    return TenantBlock(tenant_id=_UNASSIGNED, project_id=_UNASSIGNED)


class _ProductContext:
    def __init__(self, model: dict, request: ScrapeJobRequest):
        self.model = model
        self.flat = model.get("specifications_flat", {})
        self.product_id = model.get("product_id") or model.get("item_id") or "product"
        self.subject = model.get("product_name")
        self.product_url = model.get("url")
        self.scraped_at = model.get("scraped_at") or _now_iso()
        self.brand = model.get("brand")
        self.sku = model.get("sku")
        self.description = model.get("description")

        self.price = model.get("price", {}) or {}
        self.currency = self.price.get("currency") or "INR"
        self.ratings = model.get("ratings", {}) or {}
        self.offers = model.get("offers", {}) or {}
        self.policies = model.get("warranty_and_policies", {}) or {}
        self.delivery = model.get("delivery", {}) or {}

        self.model_number = parsing.find_spec(self.flat, "Model Number", "Model Name", "Model")
        self.color = parsing.find_spec(self.flat, "Color", "Colour")
        self.capacity = parsing.parse_capacity(
            parsing.find_spec(self.flat, "Capacity", "Total Capacity", "Refrigerator Capacity"))
        self.energy_rating = parsing.parse_energy_rating(
            parsing.find_spec(self.flat, "Energy Rating", "Star Rating", "BEE Rating"))
        self.warranty_months, self.warranty_type = parsing.parse_warranty(model)

    def entities(self) -> EntitiesBlock:
        brands = [BrandEntity(brand_name=self.brand)] if self.brand else []
        products = [ProductEntity(
            product_id=self.product_id, product_name=self.subject,
            sku=self.sku, model_number=self.model_number, product_url=self.product_url,
        )]
        return EntitiesBlock(
            brands=brands, products=products,
            retailer=RetailerEntity(retailer_id="RETAILER_FLIPKART", retailer_name="Flipkart"),
        )

    def product_attributes(self) -> ProductAttributesBlock:
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=Capacity(**self.capacity) if self.capacity else None,
            color=self.color,
            energy_rating=self.energy_rating,
            specifications=self.model.get("specifications", {}) or {},
        )

    def commercial(self) -> CommercialBlock:
        price = self.price
        listed = price.get("selling_price")
        original = price.get("mrp")
        final_selling = price.get("special_deal_price") or listed
        disc_amt = disc_pct = None
        if original and final_selling and original > final_selling:
            disc_amt = round(original - final_selling, 2)
            disc_pct = round(disc_amt / original * 100, 2)
        elif price.get("discount_percent"):
            disc_pct = parsing.to_number(price.get("discount_percent"))
        offers = self.offers
        is_promo = bool(disc_amt or disc_pct or offers.get("bank_offers"))
        free_delivery = self.delivery.get("free_delivery")

        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(listed, self.currency),
                selling_price=_money(final_selling, self.currency),
                original_price=_money(original, self.currency),
                discount_amount=disc_amt,
                discount_percentage=disc_pct,
                price_type="promotional" if is_promo else "regular",
                price_observed_at=self.scraped_at,
            ),
            price_range=PriceRangeBlock(currency=self.currency),
            promotion=PromotionBlock(
                is_promotional=is_promo,
                promotion_type=("discount" if (disc_amt or disc_pct)
                                else ("bank_offer" if offers.get("bank_offers") else None)),
                coupon_available=bool(offers.get("coupon_mentioned")),
            ),
            payment_options=PaymentOptionsBlock(
                emi_available=bool(offers.get("emi_offers")),
                no_cost_emi=bool(offers.get("no_cost_emi_mentioned")),
                bank_offer_available=bool(offers.get("bank_offers")),
                exchange_offer_available=bool(offers.get("exchange_offer")),
                cash_on_delivery=bool(self.policies.get("cash_on_delivery")),
            ),
            availability=AvailabilityBlock(status=parsing.availability_status(price.get("availability"))),
            delivery=DeliveryBlock(
                delivery_charge=_money(0, self.currency) if free_delivery else None,
                delivery_type="free_delivery" if free_delivery else None,
            ),
            warranty=WarrantyBlock(duration_months=self.warranty_months, warranty_type=self.warranty_type)
            if (self.warranty_months or self.warranty_type) else None,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        category = request.search.category
        has = bool(category)
        return TaxonomyBlock(
            category=category,
            mapping_method="search_derived" if has else "unmapped",
            mapping_status="mapped" if has else "unmapped",
        )


def _provenance(run_id: str) -> ProvenanceBlock:
    return ProvenanceBlock(
        collection_run_id=run_id,
        connector_name=CONNECTOR_NAME,
        connector_version=CONNECTOR_VERSION,
        normalizer_version=NORMALIZER_VERSION,
        ingestion_timestamp=_now_iso(),
    )


def _media_items(images, videos) -> list[MediaItem]:
    items = [MediaItem(media_type="image", url=u) for u in (images or []) if u]
    items += [MediaItem(media_type="video", url=u) for u in (videos or []) if u]
    return items


def _review_record(ctx: _ProductContext, review: dict, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    text = (review.get("review_text") or "").strip() or None
    title = (review.get("title") or "").strip() or None
    reviewer = (review.get("reviewer_name") or "").strip() or None
    posted = review.get("posted_date")
    location = (review.get("reviewer_location") or "").strip() or None
    language = detect_language(text or title)
    rating = review.get("rating")

    source_item_id = "gen:" + _hash("review", reviewer or "", posted or "", (text or "")[:80])
    content_hash = identifiers.content_hash(ctx.product_id, "review", text or "", reviewer or "", posted or "")
    record_id = identifiers.record_id(PLATFORM)

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_MENTION,
        tenant=_placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_review",
            source_type="review",
            source_item_id=source_item_id,
            source_url=ctx.product_url,
            parent_item_id=ctx.product_id,
            published_at=posted,
            collected_at=ctx.scraped_at,
            country_code="IN",
            marketplace=request.search.marketplace,
        ),
        content=ContentBlock(
            title=title,
            text=text,
            language=language,
            original_language=language,
            content_type="review",
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=rating) if rating is not None else None,
            helpful_votes=review.get("helpful_count"),
        ),
        author=AuthorBlock(
            source_author_id_hash=_hash(reviewer) if reviewer else None,
            display_name=reviewer,
            author_type="customer",
            is_verified_purchase=bool(review.get("verified_purchase", False)),
        ),
        location=LocationBlock(
            country_code="IN",
            city=location,
            location_precision="city" if location else "country",
        ),
        campaign=CampaignBlock(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=_provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def _listing_record(ctx: _ProductContext, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    content_hash = identifiers.content_hash(ctx.product_id, ctx.subject or "")
    record_id = identifiers.record_id(PLATFORM)
    rating = ctx.ratings.get("average_rating")

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=_placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_listing",
            source_type="product",
            source_item_id=ctx.product_id,
            source_url=ctx.product_url,
            collected_at=ctx.scraped_at,
            country_code=None,
            marketplace=request.search.marketplace,
        ),
        content=ContentBlock(
            title=ctx.subject,
            text=ctx.description or ctx.subject,
            language=detect_language(ctx.description or ctx.subject),
            content_type="product_listing",
            media=_media_items(ctx.model.get("images"), ctx.model.get("videos")),
            keywords=ctx.model.get("features", []) or [],
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=rating) if rating is not None else None,
            review_count=ctx.ratings.get("total_reviews"),
        ),
        author=AuthorBlock(),
        location=LocationBlock(),
        campaign=CampaignBlock(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=_provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    records: list[SocialMentionRecord] = []
    window = resolve_window(request.scraping_options.date_range)

    for review in model.get("reviews", []) or []:
        posted = review.get("posted_date")
        pd = None
        if posted:
            try:
                pd = date.fromisoformat(posted)
            except ValueError:
                pd = None
        if not in_window(pd, window):
            continue
        try:
            records.append(_review_record(ctx, review, request, run_id))
        except Exception as exc:
            _log.warning("event=review_map_failed product=%s error=%s", ctx.product_id, exc)

    if not records:
        records.append(_listing_record(ctx, request, run_id))
    return records
