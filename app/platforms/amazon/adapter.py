from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms.amazon import mapper
from app.platforms.amazon.engine import AmazonEngine, AmazonEngineConfig
from app.platforms.amazon.options import AmazonOptions, config_schema, resolve_marketplace
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

_log = get_logger("amazon.adapter")


class AmazonAdapter(PlatformAdapter):
    name = "amazon"

    def config_schema(self) -> PlatformConfigSchema:
        return config_schema()

    def validate_options(self, raw: dict) -> AmazonOptions:
        return AmazonOptions(**(raw or {}))

    def validate_request(self, request: ScrapeJobRequest) -> None:
        mp = resolve_marketplace(request.search.marketplace)
        currency = request.filters.currency
        if currency and currency.upper() != mp.currency:
            raise ValueError(
                f"filters.currency {currency!r} does not match {mp.domain} ({mp.currency}); "
                "prices are filtered in the marketplace currency and never converted"
            )

    async def scrape(
        self, request: ScrapeJobRequest, options: AmazonOptions, run_id: str
    ) -> ScrapeResult:
        engine = AmazonEngine(self._engine_config(request, options))
        models = await engine.run()
        kept = [m for m in models if self._passes_filters(m, request, options)]
        _log.info("event=scrape_done extracted=%d kept_after_filters=%d pages=%d failures=%d",
                  len(models), len(kept), engine.pages_processed, len(engine.failures))
        warnings = [f"{f['asin']}: {f['error']}" for f in engine.failures]
        if engine.aborted:
            warnings.append(f"run stopped early ({engine.aborted}); partial results kept")
        if engine.review_login_required:
            warnings.append(
                f"review pages required sign-in for {engine.review_login_required} product(s); "
                "only product-page reviews were collected"
            )
        return ScrapeResult(
            raw_products=kept,
            warnings=warnings,
            run_meta=self._run_meta(request, options, engine, models, kept),
        )

    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]:
        records: list[SocialMentionRecord] = []
        for model in result.raw_products:
            records.extend(mapper.build_records(model, request, run_id))
        return records

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _search_text(request: ScrapeJobRequest) -> str:
        s = request.search
        terms = [s.product, s.brand, *s.keywords]
        return " ".join(t.strip() for t in terms if t and t.strip())

    def _engine_config(self, request: ScrapeJobRequest, options: AmazonOptions) -> AmazonEngineConfig:
        filters, opts = request.filters, request.scraping_options
        return AmazonEngineConfig(
            marketplace=resolve_marketplace(request.search.marketplace),
            search_text=self._search_text(request),
            sort_by=opts.sort_by.value,
            min_price=float(filters.min_price) if filters.min_price is not None else None,
            max_price=float(filters.max_price) if filters.max_price is not None else None,
            include_sponsored=options.include_sponsored,
            exclude_keywords=filters.exclude_keywords,
            max_pages=opts.max_pages,
            max_products=opts.max_products,
            review_pages=opts.review_pages_per_product,
            review_sort=options.review_sort.value,
            verified_reviews_only=options.verified_reviews_only,
            detail_concurrency=settings.amazon_detail_concurrency,
            max_retries=settings.amazon_max_retries,
            min_delay_seconds=settings.amazon_min_delay_seconds,
            max_delay_seconds=settings.amazon_max_delay_seconds,
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            profile_dir=settings.amazon_profile_dir,
            nav_timeout_ms=settings.nav_timeout_ms,
            captcha_wait_seconds=settings.captcha_wait_seconds,
            debug_dir=settings.debug_dir,
        )

    @staticmethod
    def _passes_filters(model: dict, request: ScrapeJobRequest, options: AmazonOptions) -> bool:
        detail = model["detail"]
        # The price refinement is also sent to Amazon; re-check the actual buy-box
        # price because search refinements can admit off-range offers. Products
        # whose price is not shown are kept (cannot be verified either way).
        price = detail["pricing"]["current_price"]
        f = request.filters
        # Only compare when the page shows the marketplace currency; Amazon can
        # display converted prices, and we never convert to compare.
        same_currency = detail["pricing"]["currency"] == resolve_marketplace(model["marketplace"]).currency
        if price is not None and same_currency:
            if f.min_price is not None and price < float(f.min_price):
                return False
            if f.max_price is not None and price > float(f.max_price):
                return False
        if options.fulfilled_by_amazon_only and detail["seller"].get("fulfilled_by_amazon") is not True:
            return False  # unknown fulfilment is excluded rather than guessed
        return True

    @staticmethod
    def _run_meta(request: ScrapeJobRequest, options: AmazonOptions, engine: AmazonEngine,
                  models: list, kept: list) -> dict:
        opts, filters = request.scraping_options, request.filters
        price_requested = filters.min_price is not None or filters.max_price is not None
        return {
            "requested": {
                "marketplace": engine.cfg.marketplace.domain,
                "search_text": engine.cfg.search_text,
                "max_products": opts.max_products,
                "max_pages": opts.max_pages,
                "review_pages_per_product": opts.review_pages_per_product,
                "sort_by": opts.sort_by.value,
                "date_range": opts.date_range.type.value,
                "currency": filters.currency,
                "min_price": float(filters.min_price) if filters.min_price is not None else None,
                "max_price": float(filters.max_price) if filters.max_price is not None else None,
                "exclude_keywords": filters.exclude_keywords,
                "include_sponsored": options.include_sponsored,
                "fulfilled_by_amazon_only": options.fulfilled_by_amazon_only,
                "review_sort": options.review_sort.value,
                "verified_reviews_only": options.verified_reviews_only,
            },
            "actual": {
                "products_extracted": len(models),
                "products_kept": len(kept),
                "search_pages_processed": engine.pages_processed,
                "product_failures": engine.failures,
                "search_cards_skipped": engine.skipped,
                "signed_in": engine.signed_in,
                "stopped_early_reason": engine.aborted,
                "review_pages_login_required": engine.review_login_required,
            },
            "filters_applied": {
                "price_range": "applied_at_source_and_verified_on_product_page"
                if price_requested else "not_requested",
                "exclude_keywords": "applied_on_search_title" if filters.exclude_keywords else "not_requested",
                "include_sponsored": "included" if options.include_sponsored else "sponsored_excluded",
                "fulfilled_by_amazon_only": "applied" if options.fulfilled_by_amazon_only else "not_requested",
                "verified_reviews_only": "applied" if options.verified_reviews_only else "not_requested",
                "date_range": "applied_on_review_date",
            },
            "notes": {
                "currency": "prices recorded in the currency the page displays (Amazon may show a "
                            "converted price for the delivery location); no conversion performed",
                "date_range": "reviews without a parseable date are kept",
                "reviews": "Amazon serves review pages only to signed-in sessions; sign-in is never "
                           "automated. Without it, only reviews shown on the product page are collected.",
            },
        }
