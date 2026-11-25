from __future__ import annotations

import re

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.categories import RECORD_TYPE_LISTING, RECORD_TYPE_MENTION
from app.platforms.common import (
    clean_text,
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
from app.platforms.samsung import parsing
from app.processing.language import detect_language
from app.schemas.record import (
    AuthorBlock,
    AvailabilityBlock,
    BrandEntity,
    Capacity,
    CommercialBlock,
    ContentBlock,
    DeduplicationBlock,
    EntitiesBlock,
    LifecycleBlock,
    LocationBlock,
    MediaItem,
    MetricsBlock,
    MoneyAmount,
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

PLATFORM = "samsung"
CONNECTOR_NAME = "samsung_connector"
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"

_log = get_logger("samsung.mapper")


def _money(amount, currency) -> MoneyAmount | None:
    return MoneyAmount(amount=amount, currency=currency) if amount is not None else None


def _find(flat: dict, *names):
    for want in names:
        for k, v in (flat or {}).items():
            if k.split(" | ", 1)[-1].strip().lower() == want.lower() and v:
                return v
    return None


def _capacity(raw):
    if not raw:
        return None
    m = re.search(r"([\d.]+)\s*([A-Za-z]+)", str(raw))
    if not m:
        return None
    unit = m.group(2).lower()
    unit = {"l": "litre", "ltr": "litre", "litres": "litre", "liter": "litre"}.get(unit, unit)
    return Capacity(value=parsing._to_number(m.group(1)), unit=unit)


def _energy(raw):
    if not raw:
        return None
    m = re.search(r"([1-5])\s*[-]?\s*star", str(raw), re.I)
    return f"{m.group(1)}_star" if m else None


class _ProductContext:
    def __init__(self, model: dict):
        self.model = model
        self.flat = model.get("specifications_flat", {}) or {}
        self.product_id = model.get("product_id") or "product"
        self.subject = clean_text(model.get("product_name"))
        self.product_url = model.get("url")
        self.description = clean_text(model.get("description"))
        self.scraped_at = model.get("scraped_at") or now_iso()
        self.price = model.get("price", {}) or {}
        self.currency = self.price.get("currency") or "INR"
        self.ratings = model.get("ratings", {}) or {}
        self.introduced_date = model.get("introduced_date")

        # Source marketplace country (from the Samsung regional site, e.g. IN),
        # never the reviewer's location.
        self.source_country = model.get("source_country")
        self.marketplace = self.source_country

        # Validated in the parser; both model-number fields use the same value.
        self.model_number = model.get("model_number")
        self.color = model.get("color")
        self.capacity = _capacity(_find(self.flat, "Capacity", "Storage", "Total Capacity"))
        self.energy_rating = _energy(_find(self.flat, "Energy Rating", "Star Rating"))
        self.breadcrumbs = model.get("breadcrumbs") or []
        self.warranty_text = model.get("warranty_text")

    def entities(self) -> EntitiesBlock:
        return EntitiesBlock(
            brands=[BrandEntity(brand_name="Samsung", brand_role="client")],
            products=[ProductEntity(
                product_id=self.product_id, product_name=self.subject,
                sku=self.product_id, model_number=self.model_number,
                product_url=self.product_url,
            )],
            retailer=RetailerEntity(retailer_id="RETAILER_SAMSUNG", retailer_name="Samsung Official Store"),
        )

    def product_attributes(self) -> ProductAttributesBlock:
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=self.capacity,
            color=self.color,
            energy_rating=self.energy_rating,
            specifications=self.model.get("specifications", {}) or {},
        )

    def commercial(self) -> CommercialBlock:
        selling = self.price.get("selling_price")
        original = self.price.get("mrp")
        disc_amt = disc_pct = None
        if original and selling and original > selling:
            disc_amt = round(original - selling, 2)
            disc_pct = round(disc_amt / original * 100, 2)
        is_promo = bool(disc_amt)
        months = warranty_months(self.warranty_text)
        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(original or selling, self.currency),
                selling_price=_money(selling, self.currency),
                original_price=_money(original, self.currency),
                discount_amount=disc_amt,
                discount_percentage=disc_pct,
                price_type="promotional" if is_promo else "regular",
                price_observed_at=self.scraped_at,
            ),
            price_range=PriceRangeBlock(
                minimum=selling, maximum=selling,
                currency=self.currency if selling is not None else None,
            ),
            promotion=PromotionBlock(is_promotional=is_promo),
            availability=AvailabilityBlock(status=self.price.get("availability")),
            warranty=WarrantyBlock(duration_months=months, warranty_type="manufacturer")
            if months is not None else None,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        crumbs = self.breadcrumbs
        if crumbs:
            return TaxonomyBlock(
                category=crumbs[0],
                subcategory=crumbs[1] if len(crumbs) > 1 else None,
                product_type=crumbs[-1] if len(crumbs) > 2 else None,
                mapping_method="source_breadcrumb",
                mapping_status="mapped",
            )
        category = request.search.category or self.model.get("category")
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


def _source_item_id(review: dict, product_id: str) -> str:
    rid = review.get("review_id")
    if not is_blank(rid):
        return str(rid)
    return "gen:" + sha_hash(
        product_id, "review", review.get("reviewer_name") or "",
        review.get("posted_date") or "", review.get("review_text") or "",
    )


def _review_record(ctx: _ProductContext, review: dict, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    text = None if is_blank(review.get("review_text")) else review["review_text"].strip()
    title = None if is_blank(review.get("title")) else review["title"].strip()
    reviewer = None if is_blank(review.get("reviewer_name")) else review["reviewer_name"].strip()
    posted = review.get("posted_date")
    reviewer_country = country_code(review.get("reviewer_country"))
    language = detect_language(text or title)
    rating = review.get("rating")
    record_id = identifiers.record_id(PLATFORM)

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_MENTION,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_review",
            source_type="review",
            source_item_id=_source_item_id(review, ctx.product_id),
            source_url=ctx.product_url,
            parent_item_id=ctx.product_id,
            published_at=posted,
            collected_at=ctx.scraped_at,
            country_code=ctx.source_country,
            marketplace=ctx.marketplace,
        ),
        content=ContentBlock(
            title=title, text=text, language=language,
            original_language=language, content_type="review",
            media=([MediaItem(media_type="image", url=u) for u in (review.get("images") or []) if u]
                   + [MediaItem(media_type="video", url=u) for u in (review.get("videos") or []) if u]),
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
            source_author_id_hash=sha_hash(reviewer) if reviewer else None,
            display_name=reviewer,
            author_type="customer",
            is_verified_purchase=bool(review.get("verified_purchase", False)),
        ),
        location=LocationBlock(
            country_code=reviewer_country,
            location_precision="country" if reviewer_country else None,
        ),
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(
            content_hash=identifiers.content_hash(
                ctx.product_id, "review", text or "", reviewer or "", posted or ""),
            canonical_record_id=record_id,
        ),
        provenance=ctx.provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def _listing_record(ctx: _ProductContext, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    content_hash = identifiers.content_hash(ctx.product_id, ctx.subject or "")
    record_id = identifiers.record_id(PLATFORM)
    rating = ctx.ratings.get("average_rating")
    body = ctx.description or ctx.subject

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_listing",
            source_type="product",
            source_item_id=ctx.product_id,
            source_url=ctx.product_url,
            # A listing has no publication date; the product launch date is not a
            # publication date and is intentionally not mapped here.
            published_at=None,
            collected_at=ctx.scraped_at,
            country_code=ctx.source_country,
            marketplace=ctx.marketplace,
        ),
        content=ContentBlock(
            title=ctx.subject,
            text=body,
            language=detect_language(body),
            content_type="product_listing",
            media=media_items(ctx.model.get("images"), ctx.model.get("videos")),
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
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=ctx.provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model)
    records: list[SocialMentionRecord] = []
    window = resolve_window(request.scraping_options.date_range)

    for review in model.get("reviews", []) or []:
        # Reviews outside the requested window are dropped; undated reviews kept.
        if not in_window(parse_date(review.get("posted_date")), window):
            continue
        try:
            records.append(_review_record(ctx, review, request, run_id))
        except Exception as exc:
            _log.warning("event=review_map_failed product=%s error=%s", ctx.product_id, exc)

    if not records:
        records.append(_listing_record(ctx, request, run_id))
    return records
