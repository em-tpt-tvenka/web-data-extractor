"""Map raw Amazon engine models to the unified social-mention schema.

Same contract as the Alibaba mapper: one record per extracted customer review;
a product with no review inside the requested date window yields a single
product-listing record so the product is still represented. Nothing is
fabricated: fields the page does not provide are left null.
"""

from __future__ import annotations

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.categories import (
    RECORD_TYPE_LISTING,
    RECORD_TYPE_MENTION,
    MentionKind,
    classify_mention,
)
from app.platforms.common import (
    country_code,
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
    LocationBlock,
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
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"

_log = get_logger("amazon.mapper")


def _money(amount, currency) -> MoneyAmount | None:
    return MoneyAmount(amount=float(amount), currency=currency) if amount is not None else None


class _ProductContext:
    """Normalized, reusable view of one raw product model."""

    def __init__(self, model: dict, request: ScrapeJobRequest):
        pd = model["product_details"]
        d = model["detail"]
        self.asin = pd["product_id"]
        self.title = pd["product_name"]
        self.product_url = pd["product_detail_url"]
        self.scraped_at = pd["scraped_at"]
        self.marketplace = model.get("marketplace") or request.search.marketplace
        self.d = d
        self.brand = None if is_blank(d.get("brand")) else d["brand"]
        self.model_number = None if is_blank(d.get("model_number")) else d["model_number"]
        self.breadcrumbs = d.get("breadcrumbs") or []
        self.rating = d["rating"]
        review_total = (model.get("review_collection") or {}).get("total_reviews")
        # Amazon headline count is "ratings"; the review-page count ("N with
        # reviews") is the number of written reviews when it is available.
        self.review_count = review_total if review_total is not None else self.rating.get("total_ratings")

    def commercial(self) -> CommercialBlock:
        p, dlv, promo, pay = self.d["pricing"], self.d["delivery"], self.d["promotion"], self.d["payment"]
        cur = p["currency"]
        delivery_type = None
        if dlv["delivery_cost"] == 0:
            delivery_type = "free"
        elif dlv["delivery_cost"]:
            delivery_type = "paid"
        months = warranty_months(self.d.get("warranty_text"))
        wtype = "manufacturer" if "manufactur" in (self.d.get("warranty_text") or "").lower() else None
        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(p["original_price"], cur),
                selling_price=_money(p["current_price"], cur),
                original_price=_money(p["original_price"], cur),
                discount_amount=p["discount_amount"],
                discount_percentage=p["discount_percentage"],
                price_type="retail",
                price_observed_at=self.scraped_at,
            ),
            price_range=PriceRangeBlock(
                minimum=p["current_price"], maximum=p["current_price"],
                currency=cur if p["current_price"] is not None else None,
            ),
            promotion=PromotionBlock(**promo),
            payment_options=PaymentOptionsBlock(**pay),
            availability=AvailabilityBlock(**self.d["availability"]),
            delivery=DeliveryBlock(
                delivery_charge=_money(dlv["delivery_cost"], cur),
                estimated_delivery_date=dlv["estimated_delivery"],
                delivery_type=delivery_type,
            ),
            warranty=WarrantyBlock(duration_months=months, warranty_type=wtype)
            if (months is not None or wtype) else None,
        )

    def product_attributes(self) -> ProductAttributesBlock:
        cap = self.d.get("capacity")
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=Capacity(**cap) if cap else None,
            color=self.d.get("color"),
            energy_rating=self.d.get("energy_rating"),
            specifications=self.d.get("specifications") or {},
        )

    def entities(self) -> EntitiesBlock:
        seller = self.d["seller"]
        retailer = None
        if seller.get("seller_id") or seller.get("seller_name"):
            retailer = RetailerEntity(retailer_id=seller.get("seller_id"),
                                      retailer_name=seller.get("seller_name"))
        return EntitiesBlock(
            brands=[BrandEntity(brand_name=self.brand)] if self.brand else [],
            products=[ProductEntity(product_id=self.asin, product_name=self.title,
                                    sku=self.asin, model_number=self.model_number,
                                    product_url=self.product_url)],
            retailer=retailer,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        # Amazon exposes the product's own breadcrumb; prefer it over the search
        # context. Without one, fall back to the search category (as Alibaba does).
        crumbs = self.breadcrumbs
        if crumbs:
            return TaxonomyBlock(
                category=crumbs[0],
                subcategory=crumbs[1] if len(crumbs) > 1 else None,
                product_type=crumbs[-1] if len(crumbs) > 2 else None,
                mapping_method="source_breadcrumb",
                mapping_status="mapped",
            )
        category = request.search.category
        return TaxonomyBlock(
            category=category,
            mapping_method="search_derived" if category else "unmapped",
            mapping_status="mapped" if category else "unmapped",
        )

    def provenance(self, run_id: str) -> ProvenanceBlock:
        return ProvenanceBlock(
            collection_run_id=run_id, connector_name=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION, normalizer_version=NORMALIZER_VERSION,
            ingestion_timestamp=now_iso(),
        )


def _source_item_id(review: dict, asin: str) -> str:
    """Amazon's own review id (R...) when present; otherwise a clearly-marked
    content-derived fallback. Never the product id alone."""
    if not is_blank(review.get("review_id")):
        return str(review["review_id"])
    return "gen:" + sha_hash(asin, review.get("reviewer_name") or "", review.get("date") or "",
                             review.get("text") or "")


def _review_record(ctx: _ProductContext, review: dict, request: ScrapeJobRequest,
                   run_id: str) -> SocialMentionRecord:
    source_type, channel, content_type = classify_mention(MentionKind.CUSTOMER_REVIEW)
    text = None if is_blank(review.get("text")) else review["text"].strip()
    title = None if is_blank(review.get("title")) else review["title"]
    language = detect_language(text or title)
    reviewer = None if is_blank(review.get("reviewer_name")) else review["reviewer_name"]
    cc = review.get("country_code") or country_code(review.get("country"))
    record_id = identifiers.record_id(PLATFORM)
    rating = review.get("rating")

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_MENTION,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM, channel=channel, source_type=source_type,
            source_item_id=_source_item_id(review, ctx.asin),
            source_url=ctx.product_url, parent_item_id=ctx.asin,
            published_at=review.get("date"), collected_at=ctx.scraped_at,
            country_code=cc, marketplace=ctx.marketplace,
        ),
        content=ContentBlock(
            title=title, text=text, language=language, original_language=language,
            content_type=content_type,
            media=media_items(review.get("images"), review.get("videos")),
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            # Individual review rating only; product aggregates stay on listings.
            rating=RatingMetric(value=rating) if rating is not None else None,
            helpful_votes=review.get("helpful_votes"),
        ),
        author=AuthorBlock(
            source_author_id_hash=sha_hash(reviewer) if reviewer else None,
            display_name=reviewer, author_type="customer",
            is_verified_purchase=bool(review.get("verified_purchase")),
        ),
        location=LocationBlock(country_code=cc, location_precision="country" if cc else None),
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(
            content_hash=identifiers.content_hash(ctx.asin, "customer_review", text or "",
                                                  reviewer or "", review.get("date") or ""),
            canonical_record_id=record_id,
        ),
        provenance=ctx.provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def _listing_record(ctx: _ProductContext, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    record_id = identifiers.record_id(PLATFORM)
    media = ctx.d["media"]
    description = ctx.d.get("description")
    bullets = ctx.d.get("feature_bullets") or []
    body = description or (" | ".join(bullets) if bullets else None) or ctx.title
    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM, channel="ecommerce_listing", source_type="product",
            # A listing is product-level, so its source item id is the ASIN.
            source_item_id=ctx.asin, source_url=ctx.product_url,
            collected_at=ctx.scraped_at, country_code=None, marketplace=ctx.marketplace,
        ),
        content=ContentBlock(
            title=ctx.title, text=body, language=detect_language(body),
            content_type="product_listing",
            media=media_items(media.get("images"), media.get("videos")),
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=ctx.rating["overall_rating"])
            if ctx.rating.get("overall_rating") is not None else None,
            review_count=ctx.review_count,
        ),
        author=AuthorBlock(),
        location=LocationBlock(),
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(
            content_hash=identifiers.content_hash(ctx.asin, ctx.title or ""),
            canonical_record_id=record_id,
        ),
        provenance=ctx.provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    window = resolve_window(request.scraping_options.date_range)
    records: list[SocialMentionRecord] = []
    for review in model["detail"].get("reviews") or []:
        # Reviews outside the window are dropped; undated reviews are kept.
        if not in_window(parse_date(review.get("date")), window):
            continue
        try:
            records.append(_review_record(ctx, review, request, run_id))
        except Exception as exc:  # one malformed review must not abort the product
            _log.warning("event=review_map_failed product=%s error=%s", ctx.asin, exc)
    if not records:
        records.append(_listing_record(ctx, request, run_id))
    return records
