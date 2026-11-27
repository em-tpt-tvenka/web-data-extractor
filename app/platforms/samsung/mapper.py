from __future__ import annotations

import re

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.categories import RECORD_TYPE_LISTING
from app.platforms.common import (
    best_variant_match,
    canonical_ids,
    clean_text,
    country_code,
    detect_spec_conflicts,
    effective_prices,
    empty_campaign,
    infer_industry,
    is_blank,
    media_items,
    now_iso,
    parse_date,
    parse_offer_amount,
    placeholder_tenant,
    sha_hash,
    warranty_months,
)
from app.platforms.samsung import parsing
from app.processing.language import detect_language, detect_languages
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


# Embedded-review fields: the eight shared across platforms, then Samsung-only
# ones (Bazaarvoice media, variant context, secondary/aspect ratings).
_REVIEW_KEYS = ("reviewer_name", "rating", "title", "review_text", "verified_purchase",
                "posted_date", "posted_relative", "helpful_count")
_SAMSUNG_REVIEW_KEYS = ("review_id", "reviewer_location", "country_code", "variant",
                        "sub_ratings", "images", "videos")


def _variant_mapping_fields(context_data_values, combinations: list[dict]) -> dict:
    """variant_id / platform_variant_id / variant_name / variant_attributes /
    variant_mapping for one review, matched against this product's known
    sibling SKUs (``variant_details``) via any recognizable colour/storage/
    RAM/SKU signal in the review's own Bazaarvoice ``ContextDataValues``.
    Confirmed live: Samsung's ContextDataValues carry no such signal today
    (only ``IncentivizedReview``/``Age``), so this honestly reports
    ``parent_level`` for every real review seen; never guessed."""
    parsed_attrs = parsing.review_variant_attrs(context_data_values)
    if not parsed_attrs:
        return {
            "variant_id": None, "platform_variant_id": None, "variant_name": None,
            "variant_attributes": None,
            "variant_mapping": {"status": "parent_level", "method": None, "confidence": None},
        }
    match, confidence = best_variant_match(parsed_attrs, combinations)
    if match is None:
        return {
            "variant_id": None, "platform_variant_id": None, "variant_name": None,
            "variant_attributes": parsed_attrs,
            "variant_mapping": {"status": "unknown", "method": "source_metadata", "confidence": 0.0},
        }
    return {
        "variant_id": match.get("variant_id"),
        "platform_variant_id": match.get("platform_variant_id"),
        "variant_name": match.get("variant_name"),
        "variant_attributes": match.get("attributes"),
        "variant_mapping": {
            "status": "verified" if confidence >= 0.999 else "inferred",
            "method": "source_metadata",
            "confidence": confidence,
        },
    }


def _source_item_id(review: dict, product_id: str) -> str:
    rid = review.get("review_id")
    if not is_blank(rid):
        return str(rid)
    return "gen:" + sha_hash(
        product_id, "review", review.get("reviewer_name") or "",
        review.get("posted_date") or "", review.get("review_text") or "",
    )


class _ProductContext:
    def __init__(self, model: dict, request: ScrapeJobRequest):
        self.model = model
        self.request = request
        self.flat = model.get("specifications_flat", {}) or {}
        self.product_id = model.get("product_id") or "product"
        self.subject = clean_text(model.get("product_name"))
        self.product_url = model.get("url")
        self.description = clean_text(model.get("description"))
        self.scraped_at = model.get("scraped_at") or now_iso()
        self.brand = model.get("brand") or "Samsung"
        self.sku = model.get("sku") or self.product_id
        self.price = model.get("price", {}) or {}
        self.currency = self.price.get("currency") or "INR"
        self.ratings = model.get("ratings", {}) or {}
        self.offers = model.get("offers", {}) or {}
        self.delivery = model.get("delivery", {}) or {}
        self.introduced_date = model.get("introduced_date")

        # Source marketplace country (from the Samsung regional site, e.g. IN),
        # never the reviewer's location.
        self.source_country = model.get("source_country")
        self.marketplace = request.search.marketplace or self.source_country

        self.model_number = model.get("model_number")
        self.color = model.get("color")
        self.capacity = _capacity(_find(self.flat, "Capacity", "Storage", "Total Capacity"))
        self.energy_rating = _energy(_find(self.flat, "Energy Rating", "Star Rating"))
        self.breadcrumbs = model.get("breadcrumbs") or []
        self.warranty_text = model.get("warranty_text")
        self.highlights = model.get("highlights") or []
        self.variants = model.get("variants") or []
        self.manufacturer = model.get("manufacturer") or None
        self.reviews_before_filter = 0
        self.reviews_outside_window = 0

    def entities(self) -> EntitiesBlock:
        capacity_dict = self.capacity.model_dump() if self.capacity else None
        canonical_product_id, variant_key = canonical_ids(
            self.brand, self.subject, capacity_dict, self.color)
        return EntitiesBlock(
            brands=[BrandEntity(brand_name=self.brand, brand_role="client")],
            products=[ProductEntity(
                product_id=self.product_id, product_name=self.subject,
                sku=self.sku, model_number=self.model_number,
                product_url=self.product_url,
                canonical_product_id=canonical_product_id, variant_key=variant_key,
            )],
            # Samsung is a first-party store: it is both brand and retailer.
            retailer=RetailerEntity(retailer_id="RETAILER_SAMSUNG",
                                    retailer_name="Samsung Official Store"),
        )

    def product_attributes(self) -> ProductAttributesBlock:
        specs = dict(self.model.get("specifications", {}) or {})
        if isinstance(self.variants, dict) and self.variants:
            summary = {
                name: ", ".join(o.get("option", str(o)) if isinstance(o, dict) else str(o)
                                for o in opts)
                for name, opts in self.variants.items() if isinstance(opts, list) and opts
            }
            if summary:
                specs["Available Variants"] = summary
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=self.capacity,
            color=self.color,
            energy_rating=self.energy_rating,
            highlights=self.highlights,
            variants=self.variants,
            manufacturer=self.manufacturer,
            specifications=specs,
            variant_details=self.model.get("variant_details") or [],
        )

    def _aggregate_availability(self) -> str | None:
        """The single known status when only one configuration's status is
        known; "partially_available" when ``variant_details`` shows a genuine
        mix across sibling SKUs -- confirmed live (Galaxy S25 Ultra): several
        256GB/512GB colour SKUs are out_of_stock while others (and the
        current/selected SKU) are in_stock, so reporting the whole product
        as a single status (the old behaviour, which just copied the current
        SKU's own status) silently hid that most of the family is
        unavailable."""
        def _status(vd: dict) -> str | None:
            avail = vd.get("availability")
            return avail.get("status") if isinstance(avail, dict) else avail

        statuses = {
            s for vd in (self.model.get("variant_details") or []) if (s := _status(vd))
        }
        if len(statuses) > 1:
            return "partially_available"
        if statuses:
            return next(iter(statuses))
        return self.price.get("availability")

    def commercial(self) -> CommercialBlock:
        selling = self.price.get("selling_price")
        original = self.price.get("mrp")
        disc_amt = disc_pct = None
        if original and selling and original > selling:
            disc_amt = round(original - selling, 2)
            disc_pct = round(disc_amt / original * 100, 2)
        offers = self.offers
        has_bank = bool(offers.get("bank_offers"))
        has_offer = bool(offers.get("benefits") or offers.get("no_cost_emi")
                         or has_bank or offers.get("exchange_offer"))
        is_promo = bool(disc_amt or has_offer)
        promo_type = ("discount" if disc_amt else ("bank_offer" if has_bank else None))
        months = warranty_months(self.warranty_text)
        free_delivery = bool(self.delivery.get("free_delivery"))

        # Price range: the real selling prices of every known sibling SKU
        # (never MRP mixed in) -- confirmed live (Galaxy S25 Ultra): the old
        # logic reported a "range" of this SKU's own selling price (119999)
        # to its own MRP (129999), which isn't a real range of anything
        # purchasable, and it ignored that other in-stock sibling SKUs in
        # variant_details sell as high as 139999. Falls back to the single
        # known selling price only when no variant_details exist.
        variant_prices = [
            v for vd in (self.model.get("variant_details") or [])
            if (v := ((vd.get("commercial") or {}).get("price") or {}).get("selling_price")) is not None
        ]
        if variant_prices:
            min_p, max_p = min(variant_prices), max(variant_prices)
        else:
            min_p = max_p = selling
        # Payment options are emitted only on explicit page evidence, else None.
        payment = PaymentOptionsBlock(
            emi_available=True if offers.get("emi_offers") else None,
            no_cost_emi=True if offers.get("no_cost_emi") else None,
            bank_offer_available=True if has_bank else None,
            exchange_offer_available=True if offers.get("exchange_offer") else None,
        )

        # Distinct per-offer-type effective prices - only computed from a real
        # numeric amount we can parse, never estimated from a bare boolean.
        # Samsung's bank/exchange offer signals are boolean-only today (no
        # free-text amount), so these parse calls correctly fall through to
        # None now, and will pick up real amounts automatically if Samsung's
        # offer extraction ever starts carrying parseable text.
        bank_offer_amount = parse_offer_amount(offers.get("bank_offers"), base_price=selling)
        exchange_amount = parse_offer_amount(offers.get("exchange_offer"), base_price=selling)
        coupon_amount = None  # No coupon/promotion text source exists on Samsung yet.
        ep = effective_prices(selling, self.currency, bank_offer_amount=bank_offer_amount,
                              coupon_amount=coupon_amount, exchange_amount=exchange_amount)

        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(original or selling, self.currency),
                selling_price=_money(selling, self.currency),
                original_price=_money(original, self.currency),
                discount_amount=disc_amt,
                discount_percentage=disc_pct,
                price_type="promotional" if is_promo else "regular",
                price_observed_at=self.scraped_at,
                lowest_price_with_offers=_money(self.price.get("lowest_with_offers"), self.currency),
                bank_offer_adjusted_price=MoneyAmount(**ep["bank_offer_adjusted_price"])
                if ep["bank_offer_adjusted_price"] else None,
                coupon_adjusted_price=MoneyAmount(**ep["coupon_adjusted_price"])
                if ep["coupon_adjusted_price"] else None,
                exchange_adjusted_price=MoneyAmount(**ep["exchange_adjusted_price"])
                if ep["exchange_adjusted_price"] else None,
                final_effective_price=MoneyAmount(**ep["final_effective_price"])
                if ep["final_effective_price"] else None,
            ),
            price_range=PriceRangeBlock(
                minimum=min_p, maximum=max_p,
                currency=self.currency if min_p is not None else None,
            ),
            promotion=PromotionBlock(is_promotional=is_promo, promotion_type=promo_type),
            payment_options=payment if any(payment.model_dump().values()) else None,
            availability=AvailabilityBlock(status=self._aggregate_availability()),
            delivery=DeliveryBlock(delivery_charge=_money(0, self.currency) if free_delivery else None,
                                   delivery_type="free_delivery" if free_delivery else None)
            if free_delivery else None,
            warranty=WarrantyBlock(duration_months=months, warranty_type="manufacturer")
            if months is not None else None,
            offers_detail=offers or None,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        crumbs = [c for c in self.breadcrumbs if c and c.lower() not in ("home", "buy")]
        if crumbs:
            cat = crumbs[0]
            subcat = crumbs[1] if len(crumbs) > 1 else None
            ptype = crumbs[-1] if len(crumbs) > 2 else (subcat or cat)
            return TaxonomyBlock(
                industry=infer_industry(" ".join(crumbs)),
                category=cat, subcategory=subcat, product_type=ptype,
                mapping_method="source_breadcrumb", mapping_status="mapped",
            )
        category = self.model.get("category")
        return TaxonomyBlock(
            industry=infer_industry(category or self.subject or ""),
            category=category,
            product_type=self.subject,
            mapping_method="search_derived" if category else "unmapped",
            mapping_status="mapped" if category else "unmapped",
        )

    def provenance(self, run_id: str) -> ProvenanceBlock:
        return ProvenanceBlock(
            collection_run_id=run_id, connector_name=CONNECTOR_NAME,
            connector_version=CONNECTOR_VERSION, normalizer_version=NORMALIZER_VERSION,
            ingestion_timestamp=now_iso(),
        )

    def reviews(self, request: ScrapeJobRequest) -> list[dict]:
        """Embedded reviews (Flipkart/Amazon shape) within the request's date
        window (undated reviews kept), de-duplicated by review id, each carrying
        its own media / variant / aspect ratings."""
        window = resolve_window(request.scraping_options.date_range)
        raw = self.model.get("reviews") or []
        self.reviews_before_filter = len(raw)
        self.reviews_outside_window = 0
        combinations = self.model.get("variant_details") or []
        out, seen = [], set()
        for r in raw:
            try:
                if not in_window(parse_date(r.get("posted_date")), window):
                    self.reviews_outside_window += 1
                    continue
                rid = _source_item_id(r, self.product_id)
                if rid in seen:
                    continue
                seen.add(rid)
                entry = {k: r.get(k) for k in _REVIEW_KEYS}
                entry["review_text"] = (entry["review_text"] or "").strip() or None
                entry["title"] = (entry["title"] or "").strip() or None
                entry["reviewer_name"] = (entry["reviewer_name"] or "").strip() or None
                entry["verified_purchase"] = bool(entry["verified_purchase"])
                entry.update({k: r.get(k) for k in _SAMSUNG_REVIEW_KEYS})
                entry["review_id"] = rid
                entry["country_code"] = country_code(r.get("reviewer_country") or r.get("country_code"))
                entry["images"] = [u for u in (r.get("images") or []) if u]
                entry["videos"] = [u for u in (r.get("videos") or []) if u]
                entry["reviewer_id_hash"] = sha_hash(entry["reviewer_name"]) if entry["reviewer_name"] else None
                entry["parent_product_id"] = self.product_id
                entry.update(_variant_mapping_fields(r.get("variant"), combinations))
                out.append(entry)
            except Exception as exc:
                _log.warning("event=review_map_failed product=%s error=%s", self.product_id, exc)
        for entry, lang in zip(out, detect_languages([e["review_text"] or e["title"] for e in out])):
            entry["language"] = lang
        return out

    def review_summary(self, kept_reviews: int, request: ScrapeJobRequest) -> dict | None:
        summary = dict(self.model.get("review_summary") or {})
        if not summary:
            return None
        summary["reviews_collected"] = kept_reviews
        summary["reviews_outside_date_range"] = self.reviews_outside_window
        summary["reviews_before_date_filter"] = self.reviews_before_filter
        summary["reviews_in_output"] = kept_reviews
        summary["date_window"] = request.scraping_options.date_range.type.value
        return summary


def _listing_record(ctx: _ProductContext, request: ScrapeJobRequest, run_id: str) -> SocialMentionRecord:
    content_hash = identifiers.content_hash(ctx.product_id, ctx.subject or "")
    record_id = identifiers.record_id(PLATFORM)
    rating = ctx.ratings.get("average_rating")
    total_ratings = ctx.ratings.get("total_ratings")
    total_reviews = ctx.ratings.get("total_reviews")
    breakdown = ctx.ratings.get("breakdown") or {}
    body = ctx.description or ctx.subject
    reviews = ctx.reviews(request)
    attrs = ctx.product_attributes()
    spec_conflicts = detect_spec_conflicts(ctx.subject, attrs.specifications)

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
            keywords=[],
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=attrs,
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
        provenance=ctx.provenance(run_id),
        quality=QualityBlock(validation_errors=spec_conflicts) if spec_conflicts else QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
        review_summary=ctx.review_summary(len(reviews), request),
        reviews=reviews,
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    # One master product-listing record per product (as Amazon / Flipkart):
    # specifications with variants, review summary and the customer reviews
    # embedded, so product details are not repeated once per review.
    return [_listing_record(ctx, request, run_id)]
