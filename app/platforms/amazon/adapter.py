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
    supports_incremental = True

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
        self, request: ScrapeJobRequest, options: AmazonOptions, run_id: str, hooks=None
    ) -> ScrapeResult:
        engine = AmazonEngine(self._engine_config(request, options))
        engine.hooks = hooks
        # Only products this adapter keeps are stored incrementally.
        engine.emit_filter = lambda m: self._passes_filters(m, request, options)
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
        if engine.throttle_events:
            warnings.append(
                f"Amazon returned its 503 'rush hour' throttling page {engine.throttle_events} time(s); the "
                "scraper backed off and slowed down. Repeated throttling means this session/IP is "
                "rate-limited - wait before the next run."
            )
        if engine.products_reviews_blocked:
            warnings.append(
                f"Amazon refused review pages ('Page Not Found' on valid review URLs) for "
                f"{engine.products_reviews_blocked} product(s) - this session is rate-limited for reviews. "
                "Product details and reviews collected before the block are kept; wait before the next run."
            )
        if engine.reviews_incomplete:
            warnings.append(
                f"{engine.reviews_incomplete} product(s) have fewer extracted reviews than Amazon reports; "
                "see review_summary.coverage_percent / incomplete_reason"
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
        # Amazon matches every word of the query, so a repeated word ("Lenovo"
        # in each of 36 keywords) adds nothing but length; keep each word once.
        words, seen = [], set()
        for t in terms:
            for w in (t or "").split():
                if w.lower() not in seen:
                    seen.add(w.lower())
                    words.append(w)
        return " ".join(words)

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
            review_sort=options.review_sort.value,
            verified_reviews_only=options.verified_reviews_only,
            review_click_min_delay=settings.amazon_review_click_min_delay,
            review_click_max_delay=settings.amazon_review_click_max_delay,
            keyword_stall_limit=settings.amazon_review_keyword_stall_limit,
            keyword_yield_window=settings.amazon_review_keyword_yield_window,
            keyword_min_yield=settings.amazon_review_keyword_min_yield,
            throttle_backoff_seconds=settings.amazon_throttle_backoff_seconds,
            throttle_retries=settings.amazon_throttle_retries,
            max_requests_per_minute=settings.amazon_max_requests_per_minute,
            cooldown_minutes=settings.amazon_cooldown_minutes,
            detail_concurrency=settings.amazon_detail_concurrency,
            max_retries=settings.amazon_max_retries,
            min_delay_seconds=settings.amazon_min_delay_seconds,
            max_delay_seconds=settings.amazon_max_delay_seconds,
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            profile_dir=settings.profile_for("amazon"),
            nav_timeout_ms=settings.nav_timeout_ms,
            captcha_wait_seconds=settings.captcha_wait_seconds,
            debug_dir=settings.debug_dir,
        )

    @staticmethod
    def _passes_filters(model: dict, request: ScrapeJobRequest, options: AmazonOptions) -> bool:
        price_block = model.get("price", {}) or {}
        # The price refinement is also sent to Amazon; re-check the actual buy-box
        # price because search refinements can admit off-range offers. Products
        # whose price is not shown are kept (cannot be verified either way).
        price = price_block.get("special_deal_price") or price_block.get("selling_price")
        f = request.filters
        # Only compare when the page shows the marketplace currency; Amazon can
        # display converted prices, and we never convert to compare.
        same_currency = price_block.get("currency") == resolve_marketplace(model["marketplace"]).currency
        if price is not None and same_currency:
            if f.min_price is not None and price < float(f.min_price):
                return False
            if f.max_price is not None and price > float(f.max_price):
                return False
        # exclude_keywords are applied by the engine on the search-result title
        # only. Amazon's full product titles embed the spec list ("16GB RAM/512GB
        # SSD/..."), so re-checking them would drop every product when keywords
        # such as "RAM" or "SSD" are used to exclude accessories.
        if options.fulfilled_by_amazon_only and (model.get("seller") or {}).get("fulfilled_by_amazon") is not True:
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
                "products_discovered": len(models),
                "products_kept": len(kept),
                "search_pages_processed": engine.pages_processed,
                "product_failures": engine.failures,
                "search_cards_skipped": engine.skipped,
                "signed_in": engine.signed_in,
                "stopped_early_reason": engine.aborted,
                "review_pages_login_required": engine.review_login_required,
                "reviews_available": sum((m.get("review_summary") or {}).get("reviews_available") or 0
                                         for m in kept),
                "reviews_extracted": sum(len(m.get("reviews") or []) for m in kept),
                "products_reviews_incomplete": engine.reviews_incomplete,
                "throttle_events": engine.throttle_events,
                "review_page_refusals": engine.review_block_events,
                "products_reviews_blocked": engine.products_reviews_blocked,
            },
            "performance": {
                **engine.stats.summary(engine.requests_sent),
                "settings": {"product_concurrency": engine.cfg.detail_concurrency,
                             "page_delay_seconds": [engine.cfg.min_delay_seconds, engine.cfg.max_delay_seconds],
                             "show_more_delay_seconds": [engine.cfg.review_click_min_delay,
                                                         engine.cfg.review_click_max_delay],
                             "max_retries": engine.cfg.max_retries,
                             "request_timeout_ms": engine.cfg.nav_timeout_ms},
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
                "review_coverage": "Amazon shows at most 100 reviews per review listing; the listings "
                                   "are partitioned (star, sort, media, format, verified, keyword) to "
                                   "reach every accessible review. Each "
                                   "product's review_summary compares reviews_available with "
                                   "reviews_extracted; reviews_extracted counts before the date_range filter.",
            },
        }
