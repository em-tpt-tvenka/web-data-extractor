from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.platforms.samsung import mapper
from app.platforms.samsung.engine import EngineConfig, SamsungEngine
from app.platforms.samsung.options import SamsungOptions, config_schema
from app.schemas.config_schema import PlatformConfigSchema
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

_log = get_logger("samsung.adapter")


class SamsungAdapter(PlatformAdapter):
    name = "samsung"

    def config_schema(self) -> PlatformConfigSchema:
        return config_schema()

    def validate_options(self, raw: dict) -> SamsungOptions:
        return SamsungOptions(**(raw or {}))

    async def scrape(
        self, request: ScrapeJobRequest, options: SamsungOptions, run_id: str
    ) -> ScrapeResult:
        engine = SamsungEngine(self._engine_config(request, options))
        models = await engine.run()
        kept = [m for m in models if self._passes_filters(m, request)]
        _log.info("event=scrape_done extracted=%d kept_after_filters=%d pages=%d",
                  len(models), len(kept), engine.pages_processed)
        return ScrapeResult(
            raw_products=kept,
            run_meta=self._run_meta(request, options, models, kept, engine.pages_processed),
        )

    def to_records(
        self, result: ScrapeResult, request: ScrapeJobRequest, run_id: str
    ) -> list[SocialMentionRecord]:
        records: list[SocialMentionRecord] = []
        for model in result.raw_products:
            records.extend(mapper.build_records(model, request, run_id))
        return records

    def _engine_config(self, request: ScrapeJobRequest, options: SamsungOptions) -> EngineConfig:
        search = request.search
        terms = [search.product]
        if search.brand:
            terms.append(search.brand)
        terms.extend(search.keywords)
        search_text = " ".join(t for t in terms if t).strip()

        opts = request.scraping_options
        return EngineConfig(
            search_text=search_text,
            base_url=settings.samsung_base_url,
            category=options.category,
            exclude_keywords=request.filters.exclude_keywords,
            max_pages=opts.max_pages,
            max_products=opts.max_products,
            fetch_reviews=True,
            headless=settings.browser_headless,
            browser_channel=settings.browser_channel,
            chrome_profile_dir=settings.profile_for("samsung"),
            nav_timeout_ms=settings.nav_timeout_ms,
            bv_api_url=settings.samsung_bv_api_url,
            bv_passkey=settings.samsung_bv_passkey,
            debug_dir=settings.debug_dir,
        )

    def _passes_filters(self, model: dict, request: ScrapeJobRequest) -> bool:
        filters = request.filters
        price = model.get("price", {}) or {}
        selling = price.get("selling_price")

        if filters.min_price is not None and selling is not None and selling < float(filters.min_price):
            return False
        if filters.max_price is not None and selling is not None and selling > float(filters.max_price):
            return False
        if filters.exclude_keywords:
            name = (model.get("product_name") or "").lower()
            if any(k.lower() in name for k in filters.exclude_keywords if k):
                return False
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
                "category": options.category,
            },
            "actual": {
                "products_discovered": len(models),
                "products_kept": len(kept),
                "search_pages_processed": pages_processed,
            },
            "filters_applied": {
                "brand_gate": "samsung_only (non-Samsung brand queries return empty)",
                "price_range": (
                    "applied_at_source_in_display_currency"
                    if filters.min_price is not None or filters.max_price is not None
                    else "not_requested"
                ),
                "exclude_keywords": "applied" if filters.exclude_keywords else "not_requested",
                "date_range": "applied_on_review_published_date",
            },
            "notes": {
                "currency": "prices preserved in source/display currency (INR); no conversion performed",
                "reviews": ("Bazaarvoice reviews are fetched via the passkey + product id read "
                            "from the product page and paginated to a hard safety ceiling of 60 "
                            "pages (6000 reviews). JSON-LD review nodes are merged in."),
            },
        }
