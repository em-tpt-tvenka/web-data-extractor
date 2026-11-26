from __future__ import annotations

import re

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms.alibaba import mapper
from app.platforms.alibaba.engine import AlibabaEngine, EngineConfig
from app.platforms.alibaba.options import AlibabaOptions, SupplierType, config_schema
from app.platforms.alibaba.parsing import parse_moq, resolve_price
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.platforms.common import country_code
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

    def validate_request(self, request: ScrapeJobRequest) -> None:
        """Reject jobs aimed at another site before they are queued (as Amazon
        rejects unsupported marketplaces). Alibaba has one global storefront."""
        domain = re.sub(r"^(https?://)?(www\.)?", "", (request.search.marketplace or "").strip().lower())
        domain = domain.split("/")[0]
        if domain not in ("alibaba.com", "alibaba"):
            raise ValueError(
                f"unsupported Alibaba marketplace {request.search.marketplace!r}; use 'alibaba.com'"
            )
        currency = request.filters.currency
        if currency and not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
            raise ValueError(f"filters.currency {currency!r} must be an ISO-4217 code such as 'USD'")

    async def scrape(
        self, request: ScrapeJobRequest, options: AlibabaOptions, run_id: str
    ) -> ScrapeResult:
        engine = AlibabaEngine(self._engine_config(request, options))
        models = await engine.run()
        kept = [m for m in models if self._passes_filters(m, request, options)]
        _log.info("event=scrape_done extracted=%d kept_after_filters=%d pages=%d failures=%d",
                  len(models), len(kept), engine.pages_processed, len(engine.failures))
        warnings = [f"{f['product_id']}: {f['error']}" for f in engine.failures]
        if engine.aborted:
            warnings.append(f"run stopped early ({engine.aborted}); partial results kept")
        skipped = sum(1 for m in models if m.get("detail_status") == "skipped_run_stopped")
        if engine.reviews_incomplete:
            warnings.append(
                f"{engine.reviews_incomplete} product(s) have fewer extracted reviews than Alibaba reports; "
                "see review_summary.coverage_percent / incomplete_reason (product reviews) and "
                "review_summary.store_reviews (supplier feedback)"
            )
        if skipped:
            warnings.append(f"{skipped} product(s) kept with search-card data only "
                            "(detail page not visited after the run stopped)")
        return ScrapeResult(
            raw_products=kept,
            warnings=warnings,
            run_meta=self._run_meta(request, options, engine, models, kept),
        )

    def _run_meta(
        self, request: ScrapeJobRequest, options: AlibabaOptions,
        engine: AlibabaEngine, models: list, kept: list,
    ) -> dict:
        opts = request.scraping_options
        filters = request.filters
        supplier_type_requested = options.supplier_type is not SupplierType.all
        price_requested = filters.min_price is not None or filters.max_price is not None
        detail_status: dict[str, int] = {}
        for m in models:
            s = m.get("detail_status") or "not_requested"
            detail_status[s] = detail_status.get(s, 0) + 1
        return {
            "requested": {
                "marketplace": mapper.MARKETPLACE,
                "search_text": engine.cfg.search_text,
                "max_products": opts.max_products,
                "max_pages": opts.max_pages,
                "sort_by": opts.sort_by.value,
                "date_range": opts.date_range.type.value,
                "currency": filters.currency,
                "min_price": float(filters.min_price) if filters.min_price is not None else None,
                "max_price": float(filters.max_price) if filters.max_price is not None else None,
                "exclude_keywords": filters.exclude_keywords,
                "supplier_country": options.supplier_country,
                "supplier_type": options.supplier_type.value,
                "verified_supplier_only": options.verified_supplier_only,
                "trade_assurance_only": options.trade_assurance_only,
                "min_order_quantity": options.min_order_quantity,
                "max_order_quantity": options.max_order_quantity,
                "include_sponsored": options.include_sponsored,
                "verified_reviews_only": options.verified_reviews_only,
            },
            "actual": {
                "products_discovered": len(models),
                "products_kept": len(kept),
                "search_pages_processed": engine.pages_processed,
                "product_failures": engine.failures,
                "search_cards_skipped": engine.skipped,
                "detail_pages": detail_status,
                "stopped_early_reason": engine.aborted,
                "review_pages_per_source": "all",
                "product_reviews_available": sum((m.get("review_summary") or {}).get("reviews_available") or 0
                                                 for m in kept),
                "product_reviews_extracted": sum((m.get("review_summary") or {}).get("reviews_extracted") or 0
                                                 for m in kept),
                "products_reviews_incomplete": engine.reviews_incomplete,
            },
            "filters_applied": {
                "price_range": "applied_at_source_and_verified_on_product_page"
                if price_requested else "not_requested",
                "exclude_keywords": "applied_on_search_title" if filters.exclude_keywords else "not_requested",
                "verified_supplier_only": "applied" if options.verified_supplier_only else "not_requested",
                "trade_assurance_only": "applied_at_source_and_verified_on_product_page"
                if options.trade_assurance_only else "not_requested",
                "supplier_country": "applied" if options.supplier_country else "not_requested",
                "supplier_type": "applied" if supplier_type_requested else "not_requested",
                "min_order_quantity": "applied_at_source" if options.min_order_quantity else "not_requested",
                "max_order_quantity": "applied" if options.max_order_quantity else "not_requested",
                "include_sponsored": "included" if options.include_sponsored else "sponsored_excluded",
                "verified_reviews_only": "applied" if options.verified_reviews_only else "not_requested",
                "date_range": "applied_on_review_date",
            },
            "notes": {
                "currency": "prices recorded in the currency Alibaba displays; Alibaba's own USD "
                            "figures are kept alongside; no conversion performed",
                "price_range": "a product is kept when its price range overlaps the requested range, "
                               "compared in filters.currency (display currency, or Alibaba's USD figure "
                               "when filters.currency is USD); products whose price cannot be compared "
                               "are kept",
                "date_range": "reviews without a parseable date are kept",
                "reviews": "product reviews and supplier (store) feedback are both embedded in "
                           "'reviews', told apart by review_type",
                "supplier_signals": "supplier country/type/verification are used for filtering and "
                                    "stored on entities.retailer as seller fields",
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
            verified_reviews_only=options.verified_reviews_only,
            max_retries=settings.alibaba_max_retries,
            min_delay_seconds=settings.alibaba_min_delay_seconds,
            max_delay_seconds=settings.alibaba_max_delay_seconds,
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            chrome_profile_dir=settings.profile_for("alibaba"),
            nav_timeout_ms=settings.nav_timeout_ms,
            captcha_wait_seconds=settings.captcha_wait_seconds,
            debug_dir=settings.debug_dir,
        )

    @staticmethod
    def _price_bounds(model: dict, currency: str | None) -> tuple[float, float] | None:
        """(min, max) product price in ``currency`` as the page shows it; None
        when it cannot be compared without converting."""
        price = resolve_price((model.get("product_details") or {}).get("price_range"), model.get("product_ex"))
        wanted = currency.strip().upper() if currency else None
        if price["price_min"] is not None and (wanted is None or price.get("currency") == wanted):
            return float(price["price_min"]), float(price["price_max"] if price["price_max"] is not None
                                                    else price["price_min"])
        if wanted == "USD" and price.get("usd_min") is not None:
            return float(price["usd_min"]), float(price.get("usd_max") or price["usd_min"])
        return None

    def _passes_filters(
        self, model: dict, request: ScrapeJobRequest, options: AlibabaOptions
    ) -> bool:
        seller = model.get("seller_native") or {}
        supplier = model.get("supplier_details") or {}
        f = request.filters

        # The price refinement is also sent to Alibaba; re-check against the
        # product's own price because search refinements can admit off-range
        # offers (as Amazon). Ranges must overlap; uncomparable prices are kept.
        if f.min_price is not None or f.max_price is not None:
            bounds = self._price_bounds(model, f.currency)
            if bounds is not None:
                lo, hi = bounds
                if f.max_price is not None and lo > float(f.max_price):
                    return False
                if f.min_price is not None and hi < float(f.min_price):
                    return False

        if options.verified_supplier_only:
            if supplier.get("verification_status") != "Verified":
                return False

        # Applied at source; drop only on explicit evidence from the product
        # page that the supplier does not offer Trade Assurance.
        if options.trade_assurance_only and seller.get("tradeAssurance") is False:
            return False

        if options.supplier_country:
            raw_country = seller.get("companyRegisterCountry") or supplier.get("country")
            country = country_code(raw_country)
            wanted = {country_code(c) for c in options.supplier_country} - {None}
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
