from __future__ import annotations

import re

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.alibaba import parsing
from app.platforms.common import (
    clean_text,
    country_code,
    empty_campaign,
    is_blank,
    media_items,
    now_iso,
    placeholder_tenant,
    sha_hash,
    warranty_months,
)
from app.processing.language import detect_language
from app.platforms.categories import (
    RECORD_TYPE_LISTING,
    RECORD_TYPE_MENTION,
    MentionKind,
    classify_mention,
)

PLATFORM = "alibaba"

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

CONNECTOR_NAME = "alibaba_connector"
CONNECTOR_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"

_log = get_logger("alibaba.mapper")

def _iso_date(value) -> str | None:
    d = parsing.parse_posted_date(value)
    return d.isoformat() if d else None

def _spec_lookup(specs: dict, *needles: str):
    for key, val in (specs or {}).items():
        kl = str(key).lower()
        if any(n in kl for n in needles) and val not in (None, "", "N/A"):
            return str(val)
    return None

def _parse_capacity(specs: dict) -> Capacity | None:
    raw = _spec_lookup(specs, "capacity", "volume")
    if not raw:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(litre|liter|l\b|ml|kg|g\b|ton|cu|cbm)", raw, re.I)
    if not m:
        return None
    unit = m.group(2).lower()
    unit = {"l": "litre", "liter": "litre"}.get(unit, unit)
    return Capacity(value=float(m.group(1)), unit=unit)

def _parse_warranty(raw) -> WarrantyBlock | None:
    if is_blank(raw):
        return None
    text = str(raw)
    months = warranty_months(text)
    wtype = "manufacturer" if re.search(r"manufactur", text, re.I) else None
    if months is None and wtype is None:
        return None
    return WarrantyBlock(duration_months=months, warranty_type=wtype)

class _ProductContext:
    def __init__(self, model: dict, request: ScrapeJobRequest):
        pd = model["product_details"]
        ex = model.get("product_ex") or {}

        self.offer_id = pd["product_id"]
        self.subject = pd["product_name"]
        self.product_url = pd["product_detail_url"]
        self.scraped_at = pd["scraped_at"]
        self.is_sponsored = model.get("is_sponsored", False)

        self.specs = ex.get("specifications") or {}
        brand = parsing.sanitize_brand(ex.get("brand"))
        self.brand = brand["value"]
        mnv = parsing.validate_model_number(ex)
        self.model_number = mnv["value"]
        self.colors = ex.get("colors") or []

        self.price = parsing.parse_price(pd.get("price_range"))
        tiers = ex.get("price_tiers") or []
        tier_prices = [t["price"] for t in tiers if isinstance(t.get("price"), (int, float))]
        if tier_prices:
            self.price["price_min"] = min(tier_prices)
            self.price["price_max"] = max(tier_prices)
            if not self.price.get("currency"):
                self.price["currency"] = _currency_from_pattern(ex.get("price_currency_pattern"))

        images = parsing.classify_images(
            pd["media"].get("images"), model.get("product_media"),
            (model.get("seller_native") or {}).get("companyLogo"),
        )
        self.images = images["product_image_urls"]
        self.videos = ((model.get("product_media") or {}).get("videos")
                       or pd["media"].get("videos") or [])

        self.capacity = _parse_capacity(self.specs)
        self.color = self.colors[0] if self.colors else _spec_lookup(self.specs, "color", "colour")
        self.energy_rating = _spec_lookup(self.specs, "energy")
        self.warranty = _parse_warranty(ex.get("warranty"))

        ident = parsing.resolve_supplier_identity(
            (model.get("seller_native") or {}).get("companyId"),
            model["supplier_details"].get("card_name"),
            (model.get("seller_native") or {}).get("companyName"),
        )
        self.supplier_id = ident["supplier_id"]
        self.supplier_name = ident["supplier_name"]
        self.supplier_country = (
            (model.get("seller_native") or {}).get("companyRegisterCountry")
            or model["supplier_details"].get("country")
        )
        # Source country is the supplier's registered country (where the offer
        # originates), not the reviewer's location.
        self.supplier_country_code = country_code(self.supplier_country)

        prns = model.get("product_review_summary_native") or {}
        card_rating = parsing.parse_stars(model["reviews"]["product_reviews"].get("rating"))
        self.product_rating = prns.get("average_star") if prns.get("average_star") is not None else card_rating
        self.product_review_count = prns.get("total_review_count")
        self.date_posted = _iso_date(pd.get("date_posted"))

    def price_block(self) -> PriceBlock:
        currency = self.price.get("currency")
        pmin = self.price.get("price_min")
        selling = MoneyAmount(amount=float(pmin), currency=currency) if pmin is not None else None
        return PriceBlock(
            selling_price=selling,
            price_type="wholesale",
            price_observed_at=self.scraped_at,
        )

    def price_range_block(self) -> PriceRangeBlock:
        return PriceRangeBlock(
            minimum=self.price.get("price_min"),
            maximum=self.price.get("price_max"),
            currency=self.price.get("currency"),
        )

    def commercial(self) -> CommercialBlock:
        return CommercialBlock(
            price=self.price_block(),
            price_range=self.price_range_block(),
            promotion=PromotionBlock(is_promotional=False),
            payment_options=PaymentOptionsBlock(),
            availability=AvailabilityBlock(),
            delivery=DeliveryBlock(),
            warranty=self.warranty,
        )

    def product_attributes(self) -> ProductAttributesBlock:
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=self.capacity,
            color=self.color,
            energy_rating=self.energy_rating,
            specifications=self.specs,
        )

    def entities(self, include_seller: bool) -> EntitiesBlock:
        brands = [BrandEntity(brand_name=self.brand)] if self.brand else []
        products = [ProductEntity(
            product_id=self.offer_id, product_name=self.subject,
            model_number=self.model_number, product_url=self.product_url,
        )]
        retailer = None
        if include_seller and (self.supplier_id or self.supplier_name):
            retailer = RetailerEntity(retailer_id=self.supplier_id, retailer_name=self.supplier_name)
        return EntitiesBlock(brands=brands, products=products, retailer=retailer)

def _currency_from_pattern(pattern) -> str | None:
    if not pattern:
        return None
    for sym, code in parsing._CURRENCY_SYMBOLS.items():
        if sym in str(pattern):
            return code
    return None

def _taxonomy(request: ScrapeJobRequest) -> TaxonomyBlock:
    category = request.search.category
    has_values = bool(category)
    return TaxonomyBlock(
        industry=None,
        category=category,
        subcategory=None,
        product_type=None,
        mapping_method="search_derived" if has_values else "unmapped",
        mapping_status="mapped" if has_values else "unmapped",
    )

def _source_item_id(review: dict, kind: str, reviewer, review_date, text) -> str:
    original = review.get("review_id") or review.get("id")
    if not is_blank(original):
        return str(original)
    return "gen:" + sha_hash(kind, reviewer or "", review_date or "", text or "")

def _review_record(
    ctx: _ProductContext, review: dict, kind: str,
    request: ScrapeJobRequest, run_id: str,
) -> SocialMentionRecord:
    source_type, channel, content_type = classify_mention(kind)
    text, reply = parsing.split_supplier_reply(review.get("text"))
    text = (text or "").strip()
    text = None if text in ("", "N/A") else text
    language = detect_language(text)
    text = clean_text(text)
    media = review.get("media") or review.get("review_media") or {}
    media_list = media_items(media.get("images", []), media.get("videos", []))

    reviewer = review.get("reviewer")
    reviewer = None if reviewer in (None, "", "N/A") else reviewer
    review_date = review.get("date")
    source_item_id = _source_item_id(review, kind, reviewer, review_date, text)
    content_hash = identifiers.content_hash(ctx.offer_id, kind, text or "", reviewer or "", review_date or "")
    record_id = identifiers.record_id(PLATFORM)
    rating = review.get("rating")
    author_type = "customer" if kind == MentionKind.CUSTOMER_REVIEW else "buyer"

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_MENTION,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel=channel,
            source_type=source_type,
            source_item_id=source_item_id,
            source_url=ctx.product_url,
            parent_item_id=ctx.offer_id,
            published_at=_iso_date(review_date),
            collected_at=ctx.scraped_at,
            country_code=ctx.supplier_country_code,
            marketplace=request.search.marketplace,
        ),
        content=ContentBlock(
            text=text,
            language=language,
            original_language=language,
            content_type=content_type,
            media=media_list,
        ),
        entities=ctx.entities(include_seller=True),
        taxonomy=_taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=rating) if rating is not None else None,
        ),
        author=AuthorBlock(
            source_author_id_hash=sha_hash(reviewer) if reviewer else None,
            display_name=reviewer,
            author_type=author_type,
            is_verified_purchase=bool(review.get("verified_purchase", False)),
        ),
        location=LocationBlock(
            country_code=country_code(review.get("country")),
            location_precision="country" if country_code(review.get("country")) else None,
        ),
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=ProvenanceBlock(
            collection_run_id=run_id,
            connector_name=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            normalizer_version=NORMALIZER_VERSION,
            ingestion_timestamp=now_iso(),
        ),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(
            first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at,
        ),
    )

def _listing_record(
    ctx: _ProductContext, request: ScrapeJobRequest, run_id: str
) -> SocialMentionRecord:
    source_item_id = ctx.offer_id
    content_hash = identifiers.content_hash(ctx.offer_id, ctx.subject or "")
    record_id = identifiers.record_id(PLATFORM)

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_listing",
            source_type="product",
            source_item_id=source_item_id,
            source_url=ctx.product_url,
            collected_at=ctx.scraped_at,
            country_code=ctx.supplier_country_code,
            marketplace=request.search.marketplace,
        ),
        content=ContentBlock(
            title=ctx.subject,
            text=ctx.subject,
            language=detect_language(ctx.subject),
            content_type="product_listing",
            media=media_items(ctx.images, ctx.videos),
        ),
        entities=ctx.entities(include_seller=True),
        taxonomy=_taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=ctx.product_rating) if ctx.product_rating is not None else None,
            review_count=ctx.product_review_count,
        ),
        author=AuthorBlock(),
        location=LocationBlock(),
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=ProvenanceBlock(
            collection_run_id=run_id,
            connector_name=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION,
            normalizer_version=NORMALIZER_VERSION,
            ingestion_timestamp=now_iso(),
        ),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
    )

def build_records(
    model: dict, request: ScrapeJobRequest, run_id: str
) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    records: list[SocialMentionRecord] = []

    window = resolve_window(request.scraping_options.date_range)

    def _in_range(review: dict) -> bool:
        return in_window(parsing.parse_posted_date(review.get("date")), window)

    detailed = model["reviews"].get("detailed_reviews") or {}
    product_block = detailed.get("product_reviews")
    store_block = detailed.get("store_reviews")

    def _emit(review: dict, kind: str) -> None:
        if not _in_range(review):
            return
        try:
            records.append(_review_record(ctx, review, kind, request, run_id))
        except Exception as exc:
            _log.warning("event=review_map_failed product=%s kind=%s error=%s",
                         ctx.offer_id, kind, exc)

    pq = parsing.review_quality(product_block, (product_block or {}).get("tab_count", 0) or 0)
    for review in pq["items"]:
        _emit(review, MentionKind.CUSTOMER_REVIEW)

    s_complete = bool((store_block or {}).get("feedback_complete"))
    sq = parsing.review_quality(store_block, (store_block or {}).get("tab_count", 0) or 0,
                                complete=s_complete)
    for review in sq["items"]:
        _emit(review, MentionKind.SUPPLIER_FEEDBACK)

    if not records:
        records.append(_listing_record(ctx, request, run_id))
    return records
