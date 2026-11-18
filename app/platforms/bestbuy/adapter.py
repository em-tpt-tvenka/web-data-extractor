from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.platforms.bestbuy import mapper
from app.platforms.bestbuy.engine import BestBuyEngine, EngineConfig
from app.platforms.bestbuy.options import BestBuyOptions, config_schema
from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

_log = get_logger("bestbuy.adapter")


class BestBuyAdapter(PlatformAdapter):
    name = "bestbuy"

    def config_schema(self) -> PlatformConfigSchema:
        return config_schema()

    def validate_options(self, raw: dict) -> BestBuyOptions:
        return BestBuyOptions(**(raw or {}))

    async def scrape(
        self, request: ScrapeJobRequest, options: BestBuyOptions, run_id: str
    ) -> ScrapeResult:
        engine = BestBuyEngine(self._engine_config(request))
        models = await engine.run()
        kept = [m for m in models if self._passes_filters(m, request)]
        _log.info("event=scrape_done extracted=%d kept_after_filters=%d pages=%d",
                  len(models), len(kept), engine.pages_processed)
        return ScrapeResult(
            raw_products=kept,
            run_meta=self._run_meta(request, models, kept, engine.pages_processed),
        )

    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]:
        records: list[SocialMentionRecord] = []
        for model in result.raw_products:
            records.extend(mapper.build_records(model, request, run_id))
        return records

    def _engine_config(self, request: ScrapeJobRequest) -> EngineConfig:
        search = request.search
        terms = [search.product]
        if search.brand:
            terms.append(search.brand)
        terms.extend(search.keywords)
        opts = request.scraping_options
        return EngineConfig(
            search_text=" ".join(t for t in terms if t).strip(),
            marketplace=search.marketplace,
            exclude_keywords=request.filters.exclude_keywords,
            max_pages=opts.max_pages,
            max_products=opts.max_products,
            fetch_reviews=True,
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            chrome_profile_dir=settings.profile_for("bestbuy"),
            nav_timeout_ms=settings.nav_timeout_ms,
            debug_dir=settings.debug_dir,
        )

    def _passes_filters(self, model: dict, request: ScrapeJobRequest) -> bool:
        filters = request.filters
        selling = (model.get("price", {}) or {}).get("selling_price")
        if filters.min_price is not None and selling is not None and selling < float(filters.min_price):
            return False
        if filters.max_price is not None and selling is not None and selling > float(filters.max_price):
            return False
        if filters.exclude_keywords:
            name = (model.get("product_name") or "").lower()
            if any(k.lower() in name for k in filters.exclude_keywords if k):
                return False
        return True

    def _run_meta(self, request, models, kept, pages_processed) -> dict:
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
            },
            "actual": {
                "products_discovered": len(models),
                "products_kept": len(kept),
                "search_pages_processed": pages_processed,
            },
            "filters_applied": {
                "price_range": ("applied_at_source_in_display_currency"
                                if filters.min_price is not None or filters.max_price is not None else "not_requested"),
                "exclude_keywords": "applied" if filters.exclude_keywords else "not_requested",
                "date_range": "applied_on_review_published_date",
            },
            "notes": {
                "currency": "prices preserved in source/display currency (USD); no conversion performed",
                "reviews": "captured from product-page JSON-LD review nodes; many Best Buy reviews are undated and kept",
            },
        }
