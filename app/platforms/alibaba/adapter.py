from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms.alibaba import mapper
from app.platforms.alibaba.engine import AlibabaEngine, EngineConfig
from app.platforms.alibaba.options import AlibabaOptions, SupplierType, config_schema
from app.platforms.alibaba.parsing import parse_moq
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

_log = get_logger("alibaba.adapter")

class AlibabaAdapter(PlatformAdapter):
    name = "alibaba"

    def config_schema(self) -> PlatformConfigSchema:
        return config_schema()

    def validate_options(self, raw: dict) -> AlibabaOptions:
        return AlibabaOptions(**(raw or {}))

    async def scrape(
        self, request: ScrapeJobRequest, options: AlibabaOptions, run_id: str
    ) -> ScrapeResult:
        engine = AlibabaEngine(self._engine_config(request, options))
        models = await engine.run()
        kept = [m for m in models if self._passes_filters(m, request, options)]
        _log.info("event=scrape_done extracted=%d kept_after_filters=%d pages=%d",
                  len(models), len(kept), engine.pages_processed)
        return ScrapeResult(
            raw_products=kept,
            run_meta=self._run_meta(request, options, models, kept, engine.pages_processed),
        )

    def _run_meta(
        self, request: ScrapeJobRequest, options: AlibabaOptions,
        models: list, kept: list, pages_processed: int,
    ) -> dict:
        opts = request.scraping_options
        filters = request.filters
        supplier_type_requested = options.supplier_type is not SupplierType.all
        return {
            "requested": {
                "max_products": opts.max_products,
                "max_pages": opts.max_pages,
                "review_pages_per_product": opts.review_pages_per_product,
                "date_range": opts.date_range.type.value,
                "currency": filters.currency,
                "min_price": float(filters.min_price) if filters.min_price is not None else None,
                "max_price": float(filters.max_price) if filters.max_price is not None else None,
                "exclude_keywords": filters.exclude_keywords,
                "supplier_country": options.supplier_country,
                "supplier_type": options.supplier_type.value,
                "verified_supplier_only": options.verified_supplier_only,
                "trade_assurance_only": options.trade_assurance_only,
                "include_sponsored": options.include_sponsored,
            },
            "actual": {
                "products_discovered": len(models),
                "products_kept": len(kept),
                "search_pages_processed": pages_processed,
            },
            "filters_applied": {
                "price_range": (
                    "applied_at_source_in_display_currency"
                    if filters.min_price is not None or filters.max_price is not None
                    else "not_requested"
                ),
                "exclude_keywords": "applied" if filters.exclude_keywords else "not_requested",
                "verified_supplier_only": "applied" if options.verified_supplier_only else "not_requested",
                "supplier_country": "applied" if options.supplier_country else "not_requested",
                "supplier_type": "applied" if supplier_type_requested else "not_requested",
                "include_sponsored": "sponsored_excluded" if not options.include_sponsored else "included",
                "date_range": "applied_on_review_published_date",
            },
            "notes": {
                "currency": "prices preserved in source/display currency; no conversion performed",
                "date_range": "reviews without a verifiable published date are flagged (missing_optional_fields) and kept",
                "supplier_signals": "supplier country/type/verification used for filtering only; not stored on records",
            },
        }

    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]:
        records: list[SocialMentionRecord] = []
        for model in result.raw_products:
            records.extend(mapper.build_records(model, request, run_id))
        return records

    def _engine_config(
        self, request: ScrapeJobRequest, options: AlibabaOptions
    ) -> EngineConfig:
        search = request.search
        terms = [search.product]
        if search.brand:
            terms.append(search.brand)
        terms.extend(search.keywords)
        search_text = " ".join(t for t in terms if t).strip()

        filters = request.filters
        opts = request.scraping_options
        return EngineConfig(
            search_text=search_text,
            sort_by=opts.sort_by.value,
            filter_verified=options.verified_supplier_only,
            filter_trade_assurance=options.trade_assurance_only,
            min_price=float(filters.min_price) if filters.min_price is not None else None,
            max_price=float(filters.max_price) if filters.max_price is not None else None,
            min_quantity=options.min_order_quantity,
            include_sponsored=options.include_sponsored,
            exclude_keywords=filters.exclude_keywords,
            max_pages=opts.max_pages,
            max_products=opts.max_products,
            fetch_reviews=True,
            review_concurrency=settings.review_concurrency,
            feedback_concurrency=settings.feedback_concurrency,
            feedback_max_pages=min(opts.review_pages_per_product, settings.feedback_max_pages),
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            chrome_profile_dir=settings.profile_for("alibaba"),
            nav_timeout_ms=settings.nav_timeout_ms,
            captcha_wait_seconds=settings.captcha_wait_seconds,
            debug_dir=settings.debug_dir,
        )

    def _passes_filters(
        self, model: dict, request: ScrapeJobRequest, options: AlibabaOptions
    ) -> bool:
        seller = model.get("seller_native") or {}
        supplier = model.get("supplier_details") or {}

        if options.verified_supplier_only:
            if supplier.get("verification_status") != "Verified":
                return False

        if options.supplier_country:
            raw_country = seller.get("companyRegisterCountry") or supplier.get("country")
            country = mapper._country_code(raw_country)
            wanted = {mapper._country_code(c) for c in options.supplier_country} - {None}
            if country is None or country not in wanted:
                return False

        if options.supplier_type is not SupplierType.all:
            business_type = (seller.get("companyBusinessType") or "").lower()
            needle = "manufacturer" if options.supplier_type is SupplierType.manufacturer else "trading"
            if needle not in business_type:
                return False

        if options.max_order_quantity is not None:
            ex = model.get("product_ex") or {}
            moq_value = ex.get("moq_value")
            if moq_value is None:
                moq_value = parse_moq(model["product_details"].get("moq")).get("moq_value")
            if moq_value is not None and moq_value > options.max_order_quantity:
                return False

        return True
