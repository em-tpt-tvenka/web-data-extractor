from __future__ import annotations

import hashlib
import re
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
        self.breadcrumbs = model.get("breadcrumbs") or []

        # Robust extraction for model_number, color, capacity
        model_num = parsing.find_spec(self.flat, "Model Number", "Model Name", "Model", "Shade Name", "Shade Number", "Shade")
        if not model_num and self.subject:
            m = re.search(r"\b(Galaxy\s+[A-Za-z0-9]+|[A-Za-z0-9]+-[A-Za-z0-9]+)\b", self.subject, re.I)
            if m:
                model_num = m.group(1).strip()
            else:
                m2 = re.search(r"(?:Superstay|Liquid|Matte|Vinyl)\s+([A-Za-z0-9\s]+?)(?:\s+Lipstick|\s+Liquid|\s*$)", self.subject, re.I)
                if m2:
                    model_num = m2.group(1).strip()
        self.model_number = model_num

        color = parsing.find_spec(self.flat, "Color", "Colour", "Shade", "Shade Name")
        if not color and self.subject:
            m = re.search(r"\(([^,)]+)(?:,\s*[^)]+)?\)", self.subject)
            if m:
                potential_color = m.group(1).strip()
                if not re.search(r"\d+\s*(?:gb|tb|mb|ram|rom|ml|g|kg)\b", potential_color, re.I):
                    color = potential_color
        self.color = color

        capacity_spec = parsing.find_spec(
            self.flat,
            "Internal Storage", "Storage", "Capacity", "Total Capacity", "ROM",
            "Hard Disk Capacity", "SSD Capacity", "Refrigerator Capacity",
            "Quantity", "Net Quantity", "Volume", "Weight"
        )
        if not capacity_spec and self.subject:
            m = re.search(r"\b(\d+(?:\.\d+)?)\s*(GB|TB|MB|ml|g|gm|kg|ltr|l)\b", self.subject, re.I)
            if m:
                capacity_spec = f"{m.group(1)} {m.group(2)}"
        self.capacity = parsing.parse_capacity(capacity_spec)

        self.energy_rating = parsing.parse_energy_rating(
            parsing.find_spec(self.flat, "Energy Rating", "Star Rating", "BEE Rating"))
        self.warranty_months, self.warranty_type = parsing.parse_warranty(model)
        self.highlights = model.get("features", []) or []
        raw_variants = model.get("variants")
        self.variants = raw_variants if raw_variants else []
        self.manufacturer = model.get("manufacturer") if model.get("manufacturer") else None

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
        specs = dict(self.model.get("specifications", {}) or {})

        # Clean up any non-warranty garbage in Warranty section
        if "Warranty" in specs and isinstance(specs["Warranty"], dict):
            clean_warranty = {}
            for k, v in specs["Warranty"].items():
                k_lower = k.lower()
                if any(w in k_lower for w in ("warranty", "service", "covered", "domestic", "international", "period")):
                    clean_warranty[k] = v
            if clean_warranty:
                specs["Warranty"] = clean_warranty
            else:
                specs.pop("Warranty", None)

        # Include variants in specification if present (as requested)
        if self.variants:
            variant_summary = {}
            for var_name, var_list in self.variants.items():
                if isinstance(var_list, list):
                    opts = [
                        v.get("option", str(v)) if isinstance(v, dict) else str(v)
                        for v in var_list
                    ]
                    variant_summary[var_name] = ", ".join(opts)
                elif isinstance(var_list, dict):
                    variant_summary[var_name] = ", ".join(f"{k}: {v}" for k, v in var_list.items())
                else:
                    variant_summary[var_name] = str(var_list)
            if variant_summary:
                specs["Available Variants"] = variant_summary

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

        if special and selling and special < (selling * 0.5):
            special = None

        final_selling = special or selling
        listed = original or selling or final_selling
        if listed and final_selling and listed < final_selling:
            listed = final_selling

        disc_amt = disc_pct = None
        if original and final_selling and original > final_selling:
            disc_amt = round(original - final_selling, 2)
            disc_pct = round(disc_amt / original * 100, 2)
        elif price.get("discount_percent"):
            disc_pct = parsing.to_number(price.get("discount_percent"))
        offers = self.offers
        is_promo = bool(disc_amt or disc_pct or offers.get("bank_offers"))
        free_delivery = self.delivery.get("free_delivery")

        min_p = final_selling
        max_p = original if (original and min_p and original >= min_p) else final_selling
        if min_p is not None and max_p is not None and min_p > max_p:
            min_p, max_p = max_p, min_p

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
                minimum=min_p,
                maximum=max_p,
                currency=self.currency,
            ),
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
            offers_detail=offers if offers else None,
        )

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        crumbs = [c for c in self.breadcrumbs if c and c.lower() != "home"]

        def _infer_industry(text: str) -> str:
            t = (text or "").lower()
            if any(w in t for w in ["mobile", "phone", "laptop", "computer", "tv", "television", "headphone", "audio", "camera", "electronic", "gadget", "tablet"]):
                return "Electronics"
            if any(w in t for w in ["refrigerator", "washing machine", "air conditioner", "microwave", "appliance"]):
                return "Home Appliances"
            if any(w in t for w in ["shirt", "dress", "clothing", "shoe", "footwear", "jeans", "fashion", "apparel"]):
                return "Fashion"
            if any(w in t for w in ["beauty", "cosmetic", "perfume", "skincare", "hair", "lipstick", "makeup", "lip", "kajal", "mascara", "foundation", "eyeliner", "nail", "shampoo", "lotion", "cream", "serum"]):
                return "Beauty & Personal Care"
            if any(w in t for w in ["furniture", "kitchen", "home decor", "bed"]):
                return "Home & Kitchen"
            return "Consumer Goods"

        if crumbs:
            cat = crumbs[0]
            subcat = crumbs[1] if len(crumbs) > 1 else None
            ptype = crumbs[-1] if len(crumbs) > 2 else (subcat or cat)
            industry = _infer_industry(f"{cat} {subcat or ''} {ptype}")
            return TaxonomyBlock(
                industry=industry,
                category=cat,
                subcategory=subcat,
                product_type=ptype,
                mapping_method="source_breadcrumb",
                mapping_status="mapped",
            )

        gen_name = (self.manufacturer or {}).get("generic_name") if self.manufacturer else None
        search_cat = request.search.category or ""
        search_query = request.search.product or request.search.search_text or self.subject or ""

        hint = gen_name or search_cat or search_query
        industry = _infer_industry(hint)

        hint_lower = hint.lower()
        if "mobile" in hint_lower or "phone" in hint_lower:
            cat = "Mobiles & Accessories"
            subcat = "Mobiles"
            ptype = "Smartphones"
        elif "laptop" in hint_lower or "computer" in hint_lower:
            cat = "Computers"
            subcat = "Laptops"
            ptype = "Laptops"
        elif "tv" in hint_lower or "television" in hint_lower:
            cat = "Televisions"
            subcat = "Smart TVs"
            ptype = "Televisions"
        elif "refrigerator" in hint_lower or "fridge" in hint_lower:
            cat = "Large Appliances"
            subcat = "Refrigerators"
            ptype = "Refrigerators"
        elif any(w in hint_lower for w in ["lipstick", "makeup", "lip ", "lip gloss", "lip balm", "kajal", "mascara", "foundation", "cosmetic", "beauty"]):
            cat = "Beauty and Grooming"
            subcat = "Makeup"
            ptype = "Lipstick" if ("lipstick" in hint_lower or "lip" in hint_lower) else "Cosmetics"
        elif gen_name:
            cat = gen_name
            subcat = gen_name
            ptype = gen_name
        elif search_cat:
            cat = search_cat
            subcat = None
            ptype = search_query or search_cat
        else:
            cat = search_query
            subcat = None
            ptype = search_query

        has = bool(cat)
        return TaxonomyBlock(
            industry=industry if has else None,
            category=cat if has else None,
            subcategory=subcat,
            product_type=ptype,
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
    total_ratings = ctx.ratings.get("total_ratings")
    total_reviews = ctx.ratings.get("total_reviews")
    breakdown = ctx.ratings.get("breakdown") or {}

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
            country_code="IN",
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
            review_count=total_reviews,
            overall_rating=rating,
            total_ratings=total_ratings,
            total_reviews=total_reviews,
            rating_distribution=breakdown,
        ),
        author=AuthorBlock(),
        location=None,
        campaign=CampaignBlock(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=_provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
        review_summary=ctx.model.get("review_summary") or None,
        reviews=[
            {
                "reviewer_name": r.get("reviewer_name"),
                "rating": r.get("rating"),
                "title": r.get("title"),
                "review_text": r.get("review_text"),
                "verified_purchase": r.get("verified_purchase"),
                "posted_date": r.get("posted_date"),
                "posted_relative": r.get("posted_relative"),
                "helpful_count": r.get("helpful_count"),
            }
            for r in (ctx.model.get("reviews") or [])
        ],
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    # Always return 1 master product listing record containing in-detail offers,
    # clean specifications with variants, review summary, and attached customer reviews.
    # This prevents duplicate record flooding (e.g. 20 copies of product details for 20 reviews).
    return [_listing_record(ctx, request, run_id)]
