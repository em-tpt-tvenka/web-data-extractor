

from __future__ import annotations

import re

from app.core import identifiers
from app.core.logging import get_logger
from app.core.periods import in_window, resolve_window
from app.platforms.alibaba import parsing
from app.platforms.categories import RECORD_TYPE_LISTING, MentionKind, classify_mention
from app.platforms.common import (
    clean_text,
    country_code,
    empty_campaign,
    infer_industry,
    is_blank,
    media_items,
    now_iso,
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

PLATFORM = "alibaba"
CONNECTOR_NAME = "alibaba_connector"
CONNECTOR_VERSION = "1.1.0"
NORMALIZER_VERSION = "1.1.0"
RETAILER_ID = "RETAILER_ALIBABA"
MARKETPLACE = "alibaba.com"
MARKETPLACE_NAME = "Alibaba.com"

# Keys of every embedded review: the shared (Flipkart/Amazon) eight first,
# then Alibaba-only ones.
_REVIEW_KEYS = ("reviewer_name", "rating", "title", "review_text", "verified_purchase",
                "posted_date", "posted_relative", "helpful_count")
_ALIBABA_REVIEW_KEYS = ("review_id", "review_type", "reviewer_type", "reviewer_location", "country_code",
                        "variant", "product_reviewed", "supplier_reply", "repeat_buyer", "sub_scores",
                        "images", "videos")

_log = get_logger("alibaba.mapper")


def _money(amount, currency) -> MoneyAmount | None:
    return MoneyAmount(amount=float(amount), currency=currency) if amount is not None else None


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


def _value(v):
    """Card fields use "N/A" for missing; normalise to None."""
    return None if is_blank(v) else v


def _source_item_id(review: dict, kind: str, reviewer, review_date, text) -> str:
    """Alibaba's own review id when present; otherwise a clearly-marked
    content-derived fallback over the FULL review text. Never the product id."""
    original = review.get("review_id") or review.get("id")
    if not is_blank(original):
        return str(original)
    return "gen:" + sha_hash(kind, reviewer or "", review_date or "", text or "")


class _ProductContext:
    """Normalized, reusable view of one raw Alibaba engine model."""

    def __init__(self, model: dict, request: ScrapeJobRequest):
        self.model = model
        pd = model["product_details"]
        ex = model.get("product_ex") or {}
        seller = model.get("seller_native") or {}
        supplier = model.get("supplier_details") or {}
        self.ex, self.seller, self.supplier = ex, seller, supplier

        self.offer_id = pd["product_id"]
        self.subject = pd["product_name"]
        self.product_url = pd["product_detail_url"]
        self.scraped_at = pd.get("scraped_at") or now_iso()
        self.is_sponsored = model.get("is_sponsored", False)
        self.card_moq = pd.get("moq")

        self.specs = ex.get("specifications") or {}
        brand = parsing.sanitize_brand(ex.get("brand"))
        self.brand = brand["value"]
        mnv = parsing.validate_model_number(ex)
        self.model_number = mnv["value"]
        self.colors = ex.get("colors") or []
        self.variants = parsing.variant_options(ex.get("variants"), ex.get("sku_map"),
                                                self.offer_id, self.product_url)

        self.price = parsing.resolve_price(pd.get("price_range"), ex)
        self.currency = self.price.get("currency")
        self.promotion = parsing.promotion_info(ex, self.price)
        self.availability = parsing.availability(ex)

        desc = model.get("product_description") or {}
        self.description = desc.get("description")
        self.highlights = list(desc.get("highlights") or [])

        images = parsing.classify_images(
            (pd.get("media") or {}).get("images"), model.get("product_media"), seller.get("companyLogo"),
        )
        self.images = images["product_image_urls"]
        self.videos = parsing.video_urls((model.get("product_media") or {}).get("videos")
                                         or (pd.get("media") or {}).get("videos"))

        self.capacity = _parse_capacity(self.specs)
        self.color = self.colors[0] if self.colors else _spec_lookup(self.specs, "color", "colour")
        self.energy_rating = _spec_lookup(self.specs, "energy")
        self.warranty = _parse_warranty(ex.get("warranty"))
        self.manufacturer = parsing.extract_manufacturer(ex, self.brand)
        self.breadcrumbs = parsing.clean_breadcrumbs(ex.get("category_path"))

        ident = parsing.resolve_supplier_identity(
            seller.get("companyId"), supplier.get("card_name"), seller.get("companyName"),
        )
        self.supplier_id = ident["supplier_id"]
        self.supplier_name = ident["supplier_name"]
        self.supplier_country = seller.get("companyRegisterCountry") or _value(supplier.get("country"))
        # Source country is the supplier's registered country (where the offer
        # originates), not the reviewer's location.
        self.supplier_country_code = country_code(self.supplier_country)

        prns = model.get("product_review_summary_native") or {}
        card_rating = parsing.parse_rating_value(((model.get("reviews") or {}).get("product_reviews") or {})
                                                 .get("rating"))
        avg = parsing.parse_rating_value(prns.get("average_star"))
        self.product_rating = avg if avg is not None else card_rating
        self.product_review_count = parsing.to_int(prns.get("total_review_count"))
        self.date_posted = _iso_date(pd.get("date_posted"))
        self.store_summary = model.get("store_review_summary") or {}

    # ── entities ────────────────────────────────────────────────────────────

    def retailer(self) -> RetailerEntity:
        """The platform is the retailer (as on Amazon / Flipkart); the supplier
        that lists the offer is kept alongside it as the seller."""
        seller, supplier = self.seller, self.supplier
        supplier_reviews = (self.model.get("reviews") or {}).get("supplier_reviews") or {}
        reorder = next((seller[k] for k in ("supplierReorderRate", "reorderRate", "repeatPurchaseRate",
                                            "repeatBuyerRate", "buyerRepeatRate") if not is_blank(seller.get(k))),
                       None)
        return RetailerEntity(
            retailer_id=RETAILER_ID,
            retailer_name="Alibaba",
            marketplace=MARKETPLACE,
            marketplace_name=MARKETPLACE_NAME,
            seller_id=self.supplier_id,
            seller_name=self.supplier_name,
            seller_rating=parsing.parse_rating_value(self.store_summary.get("averageStar")),
            ships_from=_value(self.ex.get("shipping_origin")),
            supplier_country=self.supplier_country,
            supplier_country_code=self.supplier_country_code,
            supplier_type=_value(seller.get("companyBusinessType")),
            verification_status=_value(supplier.get("verification_status")),
            gold_supplier=supplier.get("gold_supplier"),
            years_active=_value(seller.get("companyJoinYears")) or _value(supplier.get("years_active")),
            trade_assurance=seller.get("tradeAssurance"),
            top_rated_supplier=seller.get("topRatedSupplier"),
            response_rate=_value(supplier_reviews.get("response_rate")),
            response_time=_value(seller.get("responseTimeText")),
            on_time_delivery_rate=_value(seller.get("supplierOnTimeDeliveryRate")),
            reorder_rate=reorder,
            company_profile_url=_value(seller.get("companyProfileUrl")),
        )

    def entities(self) -> EntitiesBlock:
        brands = [BrandEntity(brand_name=self.brand)] if self.brand else []
        products = [ProductEntity(
            product_id=self.offer_id, product_name=self.subject,
            sku=self.offer_id, model_number=self.model_number, product_url=self.product_url,
        )]
        return EntitiesBlock(brands=brands, products=products, retailer=self.retailer())

    # ── product attributes ──────────────────────────────────────────────────

    def specifications(self) -> dict:
        """Sectioned like Amazon (``{"Section": {"Key": "Value"}}``); falls back
        to one section when the page did not expose its attribute groups."""
        sections = {k: dict(v) for k, v in (self.ex.get("specification_sections") or {}).items()
                    if isinstance(v, dict) and v}
        if not sections and self.specs:
            sections = {"Specifications": dict(self.specs)}
        summary = {name: ", ".join(o["option"] for o in opts) for name, opts in self.variants.items()}
        if summary:
            sections["Available Variants"] = summary
        return sections

    def product_attributes(self) -> ProductAttributesBlock:
        ex = self.ex
        return ProductAttributesBlock(
            model_number=self.model_number,
            capacity=self.capacity,
            color=self.color,
            energy_rating=self.energy_rating,
            highlights=self.highlights,  # "Highlights at a glance" bullets
            variants=self.variants,
            manufacturer=self.manufacturer,
            specifications=self.specifications(),
            # Alibaba-only attributes, read from the product's own attributes.
            material=_value(ex.get("material")),
            weight=_value(ex.get("weight")),
            dimensions=_value(ex.get("dimensions")),
            condition=_value(ex.get("condition")),
            sizes=ex.get("sizes") or [],
            certifications=ex.get("certifications") or [],
            place_of_origin=_value(ex.get("place_of_origin")),
            category_id=ex.get("category_id"),
        )

    # ── commercial ──────────────────────────────────────────────────────────

    def commercial(self) -> CommercialBlock:
        p = self.price
        pmin, pmax = p.get("price_min"), p.get("price_max")
        original = p.get("original_min")
        # Same derivation as Amazon: the lowest (tier / promotional) price is
        # the selling price, the struck-through price is the original and the
        # listed price; a discount amount / percentage the page does not state
        # is derived from the two only when the original is the higher price.
        disc_amt = None
        disc_pct = p.get("discount_percentage")
        if original is not None and pmin is not None and original > pmin:
            disc_amt = round(original - pmin, 2)
            if disc_pct is None:
                disc_pct = round(disc_amt / original * 100, 2)
        return CommercialBlock(
            price=PriceBlock(
                listed_price=_money(original if original is not None else pmin, self.currency),
                selling_price=_money(pmin, self.currency),
                original_price=_money(original, self.currency),
                discount_amount=disc_amt,
                discount_percentage=disc_pct,
                price_type="wholesale",
                price_observed_at=self.scraped_at,
                price_unit=p.get("price_unit"),
                price_raw=p.get("price_raw"),
                price_source=p.get("price_source"),
                usd_price_min=p.get("usd_min"),
                usd_price_max=p.get("usd_max"),
            ),
            price_range=PriceRangeBlock(
                minimum=pmin, maximum=pmax if pmax is not None else pmin,
                currency=self.currency if pmin is not None else None,
            ),
            promotion=PromotionBlock(**self.promotion),
            payment_options=PaymentOptionsBlock(),
            availability=AvailabilityBlock(**self.availability),
            delivery=DeliveryBlock(),
            warranty=self.warranty,
            offers_detail=parsing.wholesale_terms(self.ex, self.card_moq),
        )

    # ── taxonomy ────────────────────────────────────────────────────────────

    def taxonomy(self, request: ScrapeJobRequest) -> TaxonomyBlock:
        crumbs = self.breadcrumbs
        if crumbs:
            cat = crumbs[0]
            subcat = crumbs[1] if len(crumbs) > 1 else None
            ptype = crumbs[-1] if len(crumbs) > 2 else (subcat or cat)
            return TaxonomyBlock(
                industry=infer_industry(" ".join(crumbs)),
                category=cat, subcategory=subcat, product_type=ptype,
                mapping_method="source_breadcrumb", mapping_status="mapped",
            )
        search_cat = request.search.category
        search_query = request.search.product or self.subject
        cat = search_cat or search_query
        has = bool(cat)
        return TaxonomyBlock(
            industry=infer_industry(" ".join(filter(None, [search_cat, search_query]))) if has else None,
            category=cat if has else None,
            subcategory=None,
            product_type=search_query if has else None,
            mapping_method="search_derived" if has else "unmapped",
            mapping_status="mapped" if has else "unmapped",
        )

    # ── reviews ─────────────────────────────────────────────────────────────

    def _review_blocks(self) -> tuple[dict | None, dict | None]:
        detailed = (self.model.get("reviews") or {}).get("detailed_reviews") or {}
        return detailed.get("product_reviews"), detailed.get("store_reviews")

    def quality(self) -> tuple[dict, dict]:
        product_block, store_block = self._review_blocks()
        pq = parsing.review_quality(product_block, (product_block or {}).get("tab_count", 0) or 0)
        sq = parsing.review_quality(store_block, (store_block or {}).get("tab_count", 0) or 0,
                                    complete=bool((store_block or {}).get("feedback_complete")))
        return pq, sq

    def _embedded(self, review: dict, kind: str) -> dict:
        review_type, _, _ = classify_mention(kind)
        text, reply = parsing.split_supplier_reply(review.get("text"))
        text = None if is_blank(text) else clean_text(text)
        reply = review.get("supplier_reply") or reply
        reviewer = None if is_blank(review.get("reviewer")) else review["reviewer"]
        country = None if is_blank(review.get("country")) else review["country"]
        raw_date = None if is_blank(review.get("date")) else str(review["date"]).strip()
        rating = review.get("rating")
        media = review.get("media") or review.get("review_media") or {}
        entry = {
            "reviewer_name": reviewer,
            "rating": float(rating) if rating is not None else None,
            "title": None,  # Alibaba reviews have no title
            "review_text": text,
            "verified_purchase": bool(review.get("verified_purchase", False)),
            "posted_date": _iso_date(raw_date),
            "posted_relative": raw_date,
            "helpful_count": None,  # not shown by Alibaba
            "review_id": _source_item_id(review, kind, reviewer, raw_date, text),
            "review_type": review_type,
            "reviewer_type": "customer" if kind == MentionKind.CUSTOMER_REVIEW else "buyer",
            "reviewer_location": country,
            "country_code": country_code(country),
            "variant": parsing.sanitize_model_number(review.get("model_number")),
            "product_reviewed": review.get("product_reviewed") or None,
            "supplier_reply": clean_text(reply) if reply else None,
            "repeat_buyer": bool(review.get("repeat_buyer", False)),
            "sub_scores": review.get("latitude_scores") or [],
            "images": parsing.dedupe(media.get("images") or []),
            "videos": parsing.dedupe(media.get("videos") or []),
        }
        entry["language"] = detect_language(text)
        entry["reviewer_id_hash"] = sha_hash(reviewer) if reviewer else None
        return entry

    def reviews(self, request: ScrapeJobRequest, pq: dict, sq: dict) -> list[dict]:
        """Validated reviews (product reviews, then supplier feedback) in the
        shared embedded shape, restricted to the request's date window
        (undated reviews are kept) and de-duplicated by review id."""
        window = resolve_window(request.scraping_options.date_range)
        out, seen, seen_content = [], set(), set()
        for kind, items in ((MentionKind.CUSTOMER_REVIEW, pq["items"]),
                            (MentionKind.SUPPLIER_FEEDBACK, sq["items"])):
            for r in items:
                try:
                    if not in_window(parsing.parse_posted_date(r.get("date")), window):
                        continue
                    # Cross-source dedup: the SAME physical review can surface in
                    # both the product-review tab and the store-feedback tab and
                    # would otherwise be emitted twice with a different
                    # review_type. De-dupe on a content key (reviewer + date +
                    # text) BEFORE the type is assigned; keep the first occurrence.
                    ckey = (clean_text(r.get("reviewer") or "").lower(),
                            str(r.get("date") or "").strip(),
                            clean_text(r.get("text") or "").lower())
                    if any(ckey) and ckey in seen_content:
                        continue
                    entry = self._embedded(r, kind)
                    key = (entry["review_type"], entry["review_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    if any(ckey):
                        seen_content.add(ckey)
                    out.append(entry)
                except Exception as exc:  # one malformed review must not drop the product
                    _log.warning("event=review_map_failed product=%s kind=%s error=%s",
                                 self.offer_id, kind, exc)

        # Output-level dedup: a few store ("supplier_feedback") entries survive
        # the cross-source pass with a different review_id yet IDENTICAL final
        # review_text (the raw-text key above differs when only an appended
        # supplier reply differed). Collapse entries whose normalized
        # (reviewer_name + posted_date + review_text) match AND whose text is
        # non-empty, keeping the first. Blank-text entries are distinct
        # anonymized feedback and are always kept.
        deduped, seen_out = [], set()
        for entry in out:
            text = (entry.get("review_text") or "").strip()
            if text:
                okey = (clean_text(entry.get("reviewer_name") or "").lower(),
                        str(entry.get("posted_date") or "").strip(),
                        clean_text(text).lower())
                if okey in seen_out:
                    continue
                seen_out.add(okey)
            deduped.append(entry)
        return deduped

    def review_summary(self, reviews: list[dict], pq: dict, sq: dict,
                       request: ScrapeJobRequest) -> dict | None:
        summary = dict(self.model.get("review_summary") or {})
        if not summary:
            return None
        # Reconcile the embedded reviews[] (the windowed, de-duplicated set) with
        # the summary: the candidate count before the date filter, the count
        # remaining in the output, and the window that was applied. Keep existing
        # keys intact.
        summary["reviews_before_date_filter"] = len(pq["items"]) + len(sq["items"])
        summary["reviews_in_output"] = len(reviews)
        summary["date_window"] = request.scraping_options.date_range.type.value
        summary["reviews_collected"] = len(reviews)
        summary["product_reviews_collected"] = sum(r["review_type"] == "review" for r in reviews)
        summary["store_feedback_collected"] = sum(r["review_type"] == "supplier_feedback" for r in reviews)

        def _q(q):
            return {k: q[k] for k in ("status", "total_displayed", "extracted", "valid", "rejected",
                                      "missing_ratings", "warnings")}

        summary["quality"] = {"product_reviews": _q(pq), "store_reviews": _q(sq)}
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
    content_hash = identifiers.content_hash(ctx.offer_id, ctx.subject or "")
    record_id = identifiers.record_id(PLATFORM)
    pq, sq = ctx.quality()
    reviews = ctx.reviews(request, pq, sq)
    total_reviews = ctx.product_review_count
    if total_reviews is None:
        total_reviews = (ctx.model.get("review_summary") or {}).get("total_reviews")
    # Same body as Amazon: description, else the highlights, else the title.
    body = ctx.description or (" | ".join(ctx.highlights) if ctx.highlights else None) or ctx.subject

    return SocialMentionRecord(
        _id=record_id,
        record_type=RECORD_TYPE_LISTING,
        tenant=placeholder_tenant(),
        source=SourceBlock(
            platform=PLATFORM,
            channel="ecommerce_listing",
            source_type="product",
            source_item_id=ctx.offer_id,
            source_url=ctx.product_url,
            published_at=ctx.date_posted,
            collected_at=ctx.scraped_at,
            country_code=ctx.supplier_country_code,
            marketplace=MARKETPLACE,
        ),
        content=ContentBlock(
            title=ctx.subject,
            text=body,
            language=detect_language(body),
            content_type="product_listing",
            media=media_items(ctx.images, ctx.videos),
            # keywords are NOT the highlight bullets (those already populate
            # product_attributes.highlights); left empty unless real tags exist.
            keywords=[],
        ),
        entities=ctx.entities(),
        taxonomy=ctx.taxonomy(request),
        product_attributes=ctx.product_attributes(),
        commercial=ctx.commercial(),
        metrics=MetricsBlock(
            rating=RatingMetric(value=ctx.product_rating) if ctx.product_rating is not None else None,
            review_count=total_reviews,
            overall_rating=ctx.product_rating,
            total_ratings=None,
            total_reviews=total_reviews,
            rating_distribution={},
        ),
        author=AuthorBlock(),
        location=None,
        campaign=empty_campaign(),
        deduplication=DeduplicationBlock(content_hash=content_hash, canonical_record_id=record_id),
        provenance=_provenance(run_id),
        quality=QualityBlock(),
        lifecycle=LifecycleBlock(first_seen_at=ctx.scraped_at, last_seen_at=ctx.scraped_at),
        review_summary=ctx.review_summary(reviews, pq, sq, request),
        reviews=reviews,
    )


def build_records(model: dict, request: ScrapeJobRequest, run_id: str) -> list[SocialMentionRecord]:
    ctx = _ProductContext(model, request)
    # One master product-listing record per product (as Amazon / Flipkart):
    # wholesale terms, sectioned specifications with variants, the supplier as
    # seller, review summary and both review sources embedded.
    return [_listing_record(ctx, request, run_id)]