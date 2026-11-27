from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.categories import RECORD_TYPE_LISTING, RECORD_TYPE_MENTION
from app.platforms.common import (
    canonical_ids,
    detect_spec_conflicts,
    effective_prices,
    parse_offer_amount,
)
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
    TenantBlock,
    WarrantyBlock,
)
from app.schemas.scraping import ScrapeJobRequest

PLATFORM = "bestbuy"
CONNECTOR_NAME = "bestbuy_connector"
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"
_UNASSIGNED = "__unassigned__"

_log = get_logger("bestbuy.mapper")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode()).hexdigest()


def _money(amount, currency) -> MoneyAmount | None:
    return MoneyAmount(amount=amount, currency=currency) if amount is not None else None


def _placeholder_tenant() -> TenantBlock:
    return TenantBlock(tenant_id=_UNASSIGNED, project_id=_UNASSIGNED)


def _provenance(run_id: str) -> ProvenanceBlock:
    return ProvenanceBlock(
        collection_run_id=run_id, connector_name=CONNECTOR_NAME,
        connector_version=CONNECTOR_VERSION, normalizer_version=NORMALIZER_VERSION,
        ingestion_timestamp=_now_iso(),
    )


def _media_items(images, videos) -> list[MediaItem]:
    items = [MediaItem(media_type="image", url=u) for u in (images or []) if u]
    items += [MediaItem(media_type="video", url=u) for u in (videos or []) if u]
    return items


def _find_spec(specs: dict, *names):
    lowered = {str(k).strip().lower(): v for k, v in (specs or {}).items()}
    for want in names:
        v = lowered.get(want.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def _capacity(raw) -> Capacity | None:
    if not raw:
        return None
    m = re.search(r"([\d.]+)\s*(cu\.?\s*ft|litre|liter|l\b|ml|kg|g\b|oz|gb|tb)", str(raw), re.I)
    if not m:
        return None
    unit = m.group(2).lower().replace(".", "").replace(" ", "")
    unit = {"liter": "litre", "l": "litre", "cuft": "cu_ft"}.get(unit, unit)
    return Capacity(value=float(m.group(1)), unit=unit)


def _energy(raw) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    m = re.search(r"([1-5])\s*[- ]?star", s, re.I)
    if m:
        return f"{m.group(1)}_star"
    if re.search(r"yes|certified|true", s, re.I):
        return "energy_star_certified"
    return None


class _ProductContext:
    def __init__(self, model: dict):
        self.model = model
        self.product_id = model.get("product_id") or "product"
        self.subject = model.get("product_name")
        self.product_url = model.get("url")
        self.description = model.get("description")
        self.scraped_at = model.get("scraped_at") or _now_iso()
        self.brand = model.get("brand")
        self.price = model.get("price", {}) or {}
        self.currency = self.price.get("currency") or "USD"
        self.ratings = model.get("ratings", {}) or {}
        self.specs = model.get("specifications", {}) or {}
        self.model_number = model.get("model_number") or _find_spec(
            self.specs, "Model Number", "Model", "Manufacturer Model Number")
        self.color = _find_spec(self.specs, "Color", "Colour", "Finish")
        self.capacity = _capacity(_find_spec(self.specs, "Capacity", "Total Capacity", "Storage Capacity"))
        self.energy_rating = _energy(_find_spec(self.specs, "Energy Star Certified", "Energy Rating", "ENERGY STAR Certified"))
        self.manufacturer = model.get("manufacturer")
        self.warranty = model.get("warranty") or {}
        self.delivery = model.get("delivery") or {}
        self.trade_in = model.get("trade_in") or {}
        self.stock_quantity = model.get("stock_quantity")

    def entities(self) -> EntitiesBlock:
        brands = [BrandEntity(brand_name=self.brand)] if self.brand else []
        # Best Buy currently exposes only the single-SKU regex-extracted
        # color/capacity (no variants/colors/storage list yet), so the
        # resulting variant_key is necessarily less rich than Amazon's until
        # variant support is implemented for this platform.
        capacity_dict = self.capacity.model_dump() if self.capacity else None
        canonical_product_id, variant_key = canonical_ids(
            self.brand, self.subject, capacity_dict, self.color)
        return EntitiesBlock(
            brands=brands,
            products=[ProductEntity(product_id=self.product_id, product_name=self.subject,
                                    sku=self.product_id, model_number=self.model_number, product_url=self.product_url,
                                    canonical_product_id=canonical_product_id, variant_key=variant_key)],
            retailer=RetailerEntity(retailer_id="RETAILER_BESTBUY", retailer_name="Best Buy"),
        )

    def product_attributes(self) -> ProductAttributesBlock:
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=self.capacity,
            color=self.color,
            energy_rating=self.energy_rating,
            manufacturer=self.manufacturer,
            specifications=self.specs,
        )

    def _aggregate_availability(self) -> str | None:
        """The parent record's own status when only one configuration is
        known; "partially_available" when variant_details shows a genuine
        mix (same fix as Flipkart: don't just copy one variant's status).
        Best Buy doesn't populate variant_details yet, so this is currently
        a no-op fallback - kept consistent with every other platform so no
        further pass is needed once variants are implemented here."""
        statuses = {
            s for vd in (self.model.get("variant_details") or [])
            if (s := (vd.get("availability") or {}).get("status"))
        }
        if len(statuses) > 1:
            return "partially_available"
        if statuses:
            return next(iter(statuses))
        return self.model.get("availability")

    def commercial(self) -> CommercialBlock:
        selling = self.price.get("selling_price")
        original = self.price.get("original_price")
        disc_amt = disc_pct = None
        if original and selling and original > selling:
            disc_amt = round(original - selling, 2)
            disc_pct = round(disc_amt / original * 100, 2)
        is_promo = bool(disc_amt)

        # Price range: use the REAL selling prices of every known variant
        # (same fix as Flipkart) - only when variant_details is actually
        # populated; otherwise keep the old no-range fallback unchanged.
        variant_prices = [
            v for vd in (self.model.get("variant_details") or [])
            if (v := ((vd.get("commercial") or {}).get("price") or {}).get("selling_price")) is not None
        ]
        if variant_prices:
            price_range_block = PriceRangeBlock(
                minimum=min(variant_prices), maximum=max(variant_prices), currency=self.currency)
        else:
            price_range_block = PriceRangeBlock(currency=self.currency)

        delivery_block = None
        if self.delivery:
            delivery_block = DeliveryBlock(
                delivery_charge=_money(0, self.currency) if self.delivery.get("free_delivery") else None,
                estimated_delivery_date=self.delivery.get("estimated_delivery_date"),
                delivery_type="free_delivery" if self.delivery.get("free_delivery")
                else ("standard" if self.delivery.get("delivery_text") else None),
            )

        warranty_block = None
        if self.warranty.get("duration_months") or self.warranty.get("warranty_type"):
            warranty_block = WarrantyBlock(
                duration_months=self.warranty.get("duration_months"),
                warranty_type=self.warranty.get("warranty_type"),
            )

        offers_detail = {}
        if self.delivery.get("delivery_text"):
            offers_detail["delivery_text"] = self.delivery["delivery_text"]
        if self.warranty.get("warranty_text"):
            offers_detail["warranty_text"] = self.warranty["warranty_text"]
        if self.manufacturer and self.manufacturer.get("country_of_origin"):
            offers_detail["country_of_origin"] = self.manufacturer["country_of_origin"]
        if self.trade_in.get("trade_in_text"):
            offers_detail["trade_in_text"] = self.trade_in["trade_in_text"]
        if self.trade_in.get("trade_in_value_estimate") is not None:
            offers_detail["trade_in_value_estimate"] = self.trade_in["trade_in_value_estimate"]

        # Bank offers / no-cost EMI are not applicable to Best Buy (confirmed by
        # earlier audits), so bank_offer_amount and coupon_amount are left None
        # rather than guessed. Trade-in/exchange is the one offer type Best Buy
        # genuinely has - only wired through when the on-page trade-in text
        # carries an actual parseable numeric amount.
        exchange_amount = parse_offer_amount(self.trade_in.get("trade_in_text"), base_price=selling)
        ep = effective_prices(selling, self.currency, bank_offer_amount=None,
                              coupon_amount=None, exchange_amount=exchange_amount)

        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(original or selling, self.currency),
                selling_price=_money(selling, self.currency),
                original_price=_money(original, self.currency),
                discount_amount=disc_amt,
                discount_percentage=disc_pct,
                price_type="promotional" if is_promo else "regular",
                price_observed_at=self.scraped_at,
                bank_offer_adjusted_price=MoneyAmount(**ep["bank_offer_adjusted_price"])
                if ep["bank_offer_adjusted_price"] else None,
                coupon_adjusted_price=MoneyAmount(**ep["coupon_adjusted_price"])
                if ep["coupon_adjusted_price"] else None,
                exchange_adjusted_price=MoneyAmount(**ep["exchange_adjusted_price"])
                if ep["exchange_adjusted_price"] else None,
                final_effective_price=MoneyAmount(**ep["final_effective_price"])
                if ep["final_effective_price"] else None,
            ),
            price_range=price_range_block,
            promotion=PromotionBlock(is_promotional=is_promo),
            availability=AvailabilityBlock(status=self._aggregate_availability(),
                                           stock_quantity=self.stock_quantity),
            delivery=delivery_block,
            warranty=warranty_block,
            offers_detail=offers_detail or None,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        category = request.search.product
        has = bool(category)
        return TaxonomyBlock(
            category=category,
            mapping_method="search_derived" if has else "unmapped",
            mapping_status="mapped" if has else "unmapped",
        )


def _review_record(ctx: _ProductContext, review: dict, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    text = (review.get("review_text") or "").strip() or None
    title = (review.get("title") or "").strip() or None
    reviewer = (review.get("reviewer_name") or "").strip() or None
    posted = review.get("posted_date")
    language = detect_language(text or title)
    rating = review.get("rating")
    original = review.get("review_id")
    source_item_id = str(original) if original else "gen:" + _hash("review", reviewer or "", posted or "", (text or "")[:80])
    content_hash = identifiers.content_hash(ctx.product_id, "review", text or "", reviewer or "", posted or "")
    record_id = identifiers.record_id(PLATFORM)

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_MENTION,
        tenant=_placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM, channel="ecommerce_review", source_type="review",
            source_item_id=source_item_id, source_url=ctx.product_url, parent_item_id=ctx.product_id,
            published_at=posted, collected_at=ctx.scraped_at, country_code="US",
            marketplace=request.search.marketplace,
        ),
        content=ContentBlock(
            title=title, text=text, language=language, original_language=language, content_type="review",
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
            display_name=reviewer, author_type="customer",
            is_verified_purchase=bool(review.get("verified_purchase", False)),
        ),
        location=LocationBlock(country_code="US", location_precision="country"),
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
    attrs = ctx.product_attributes()
    spec_conflicts = detect_spec_conflicts(ctx.subject, attrs.specifications)

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=_placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM, channel="ecommerce_listing", source_type="product",
            source_item_id=ctx.product_id, source_url=ctx.product_url, collected_at=ctx.scraped_at,
            country_code=None, marketplace=request.search.marketplace,
        ),
        content=ContentBlock(
            title=ctx.subject, text=ctx.description or ctx.subject,
            language=detect_language(ctx.description or ctx.subject),
            content_type="product_listing",
            media=_media_items(ctx.model.get("images"), ctx.model.get("videos")),
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=attrs,
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
        quality=QualityBlock(validation_errors=spec_conflicts) if spec_conflicts else QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model)
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
