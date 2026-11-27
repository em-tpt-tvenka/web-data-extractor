from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.core.periods import resolve_window
from app.platforms import common
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.platforms.flipkart import mapper
from app.platforms.flipkart.engine import EngineConfig, FlipkartEngine
from app.platforms.flipkart.options import FlipkartOptions, config_schema
from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

_log = get_logger("flipkart.adapter")

class FlipkartAdapter(PlatformAdapter):
    name = "flipkart"

    def config_schema(self) -> PlatformConfigSchema:
        return config_schema()

    def validate_options(self, raw: dict) -> FlipkartOptions:
        return FlipkartOptions(**(raw or {}))

    async def scrape(
        self, request: ScrapeJobRequest, options: FlipkartOptions, run_id: str
    ) -> ScrapeResult:
        engine = FlipkartEngine(self._engine_config(request, options))
        models = await engine.run()
        kept = [m for m in models if self._passes_filters(m, request, options)]
        _log.info("event=scrape_done extracted=%d kept_after_filters=%d pages=%d",
                  len(models), len(kept), engine.pages_processed)
        warnings: list[str] = []
        if engine.reviews_incomplete:
            warnings.append(
                f"{engine.reviews_incomplete} product(s) have fewer extracted reviews than Flipkart reports; "
                "see review_summary.coverage_percent / incomplete_reason"
            )
        if engine.review_listings_failed:
            warnings.append(f"{engine.review_listings_failed} review listing(s) kept redirecting to login "
                            "and were skipped; see review_summary.failed_listings")
        run_meta = self._run_meta(request, options, models, kept, engine.pages_processed)
        summaries = [m.get("review_summary") or {} for m in kept]
        run_meta["actual"].update({
            "reviews_available": sum(s.get("reviews_available") or 0 for s in summaries),
            "reviews_extracted": sum(s.get("reviews_extracted") or 0 for s in summaries),
            "products_reviews_incomplete": engine.reviews_incomplete,
            "review_listings_failed": engine.review_listings_failed,
        })
        return ScrapeResult(raw_products=kept, warnings=warnings, run_meta=run_meta)

    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]:
        records: list[SocialMentionRecord] = []
        for model in result.raw_products:
            records.extend(mapper.build_records(model, request, run_id))
        return records

    def _engine_config(self, request: ScrapeJobRequest, options: FlipkartOptions) -> EngineConfig:
        search = request.search
        terms = [search.product]
        if search.brand:
            terms.append(search.brand)
        terms.extend(search.keywords)
        # Keep each word once (as Amazon's search_text builder already does):
        # a request with the product name repeated across several
        # overlapping "keywords" (e.g. "Galaxy S26", "Samsung S26 5G", "Galaxy
        # S26 5G Smartphone") otherwise produces a long, highly repetitive
        # query that confused Flipkart's own search relevance - confirmed
        # live it can surface mostly accessories/wrong-brand results instead
        # of the real product, where the deduped product name alone did not.
        words, seen = [], set()
        for t in terms:
            for w in (t or "").split():
                if w.lower() not in seen:
                    seen.add(w.lower())
                    words.append(w)
        search_text = " ".join(words)

        filters = request.filters
        opts = request.scraping_options
        window_start, _window_end = resolve_window(opts.date_range)
        return EngineConfig(
            search_text=search_text,
            marketplace=search.marketplace,
            sort_by=opts.sort_by.value,
            min_price=float(filters.min_price) if filters.min_price is not None else None,
            max_price=float(filters.max_price) if filters.max_price is not None else None,
            exclude_keywords=filters.exclude_keywords,
            max_pages=opts.max_pages,
            max_products=opts.max_products,
            include_sponsored=options.include_sponsored,
            fetch_reviews=True,
            review_sort=options.review_sort.value,
            verified_purchase_only=options.verified_purchase_only,
            review_window_start=window_start,
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            chrome_profile_dir=settings.profile_for("flipkart"),
            nav_timeout_ms=settings.nav_timeout_ms,
            debug_dir=settings.debug_dir,
        )

    def _passes_filters(self, model: dict, request: ScrapeJobRequest, options: FlipkartOptions) -> bool:
        filters = request.filters
        price = model.get("price", {}) or {}
        selling = price.get("lowest_price_with_offers") or price.get("selling_price")

        if filters.min_price is not None and selling is not None and selling < float(filters.min_price):
            return False
        if filters.max_price is not None and selling is not None and selling > float(filters.max_price):
            return False

        if filters.exclude_keywords:
            name = (model.get("product_name") or "").lower()
            if any(k.lower() in name for k in filters.exclude_keywords if k):
                return False

        product_name = model.get("product_name")
        if not common.matches_requested_model(product_name, request.search):
            return False  # a different model/generation than the one requested
        if common.is_accessory_listing(product_name, request.search):
            return False  # case/charger/screen-guard listing, not the product itself
        return True

    def _run_meta(self, request, options, models, kept, pages_processed) -> dict:
        opts = request.scraping_options
        filters = request.filters
        return {
            "requested": {
                "max_products": opts.max_products,
                "max_pages": opts.max_pages,
                "date_range": opts.date_range.type.value,
                "currency": filters.currency,
                "min_price": float(filters.min_price) if filters.min_price is not None else None,
                "max_price": float(filters.max_price) if filters.max_price is not None else None,
                "exclude_keywords": filters.exclude_keywords,
                "review_sort": options.review_sort.value,
                "verified_purchase_only": options.verified_purchase_only,
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
                "include_sponsored": "sponsored_excluded" if not options.include_sponsored else "included",
                "verified_purchase_only": "applied" if options.verified_purchase_only else "not_requested",
                "date_range": "applied_on_review_published_date",
            },
            "notes": {
                "currency": "prices preserved in source/display currency (INR); no conversion performed",
                "date_range": "reviews use Flipkart's relative dates (approximate for 'x months ago'); undated reviews are kept",
            },
        }
