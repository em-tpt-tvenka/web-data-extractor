from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "web-data-extractor"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    report_scope_prefix: str = "TND"

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "web_data_extractor"
    mongo_timeout_ms: int = 5000

    job_executor: str = "celery"

    celery_broker_url: str = "redis://localhost:6380/0"
    celery_result_backend: str = "redis://localhost:6380/1"

    browser_headless: bool = False
    browser_channel: str = "chrome"
    # Optional outbound proxy for every scraper browser, e.g.
    #   proxy_url=http://user:pass@us-proxy-host:port
    # Point it at the country whose storefront you need (e.g. a US proxy for
    # bestbuy.com / walmart.com when scraping from outside the US).
    proxy_url: str | None = None
    # US-only proxy, applied to walmart/bestbuy (US storefronts) while India sites
    # stay direct. Set e.g. us_proxy_url=http://user:pass@us-host:port
    us_proxy_url: str | None = None

    # Network routing for US-only storefronts. Walmart/Best Buy try the US proxy
    # first and the US VPN endpoint as fallback; every other platform stays direct.
    us_routing_enabled: bool = True
    us_proxy_enabled: bool = True
    us_vpn_enabled: bool = True
    # The VPN fallback is a dedicated US VPN-provided proxy/SOCKS endpoint (safe,
    # app-level). A true split-tunnel OS VPN is infrastructure, not done in-process.
    us_vpn_url: str | None = None
    us_vpn_mode: str = "proxy_endpoint"
    us_proxy_platforms: str = "walmart,bestbuy"
    us_expected_country: str = "US"
    us_route_timeout: int = 30
    us_route_max_retries: int = 2
    us_ip_validation_enabled: bool = True
    us_ip_check_url: str = "https://ipinfo.io/json"

    rotate_user_agents: bool = True
    # Each platform gets its own persistent Chrome profile under this dir, so warm
    # (CAPTCHA-solved / signed-in) sessions persist and multiple workers never
    # contend for the same user-data directory.
    profile_base_dir: str = str(PROJECT_ROOT / "local_data" / "profiles")
    chrome_profile_dir: str | None = str(PROJECT_ROOT / "local_data" / "chrome_scraper_profile")
    nav_timeout_ms: int = 90_000
    review_concurrency: int = 3
    feedback_concurrency: int = 2
    feedback_max_pages: int = 80
    captcha_wait_seconds: int = 300

    samsung_base_url: str = "https://www.samsung.com/in"
    samsung_bv_api_url: str | None = None
    samsung_bv_passkey: str | None = None
    
    # Amazon engine. A separate browser profile so Amazon and Alibaba jobs never
    # contend for the same Chrome user-data directory. Sign in to Amazon manually
    # once in this profile to allow full review pages (/product-reviews/), which
    # Amazon only serves to signed-in sessions; sign-in is never automated.
    amazon_profile_dir: str | None = str(PROJECT_ROOT / "local_data" / "chrome_amazon_profile")
    amazon_detail_concurrency: int = 2
    amazon_max_retries: int = 3
    amazon_min_delay_seconds: float = 2.0
    amazon_max_delay_seconds: float = 4.5
    # Review collection: pause between "Show 10 more reviews" clicks, and how
    # many keyword listings in a row may add no new review before a star
    # group is considered exhausted (stops on no progress, not on a count).
    amazon_review_click_min_delay: float = 0.8
    amazon_review_click_max_delay: float = 1.8
    amazon_review_keyword_stall_limit: int = 15
    amazon_review_keyword_yield_window: int = 8
    amazon_review_keyword_min_yield: int = 3
    # Amazon's HTTP 503 "rush hour" throttling page: back off 30 / 60 / 120 s
    # (+ jitter) and slow the request budget; stop the run after 3 refusals.
    amazon_throttle_backoff_seconds: float = 30.0
    amazon_throttle_retries: int = 3
    # Overall cap on Amazon requests (page loads + "show more") per minute,
    # across all concurrent products. Live blocks followed ~40/min sustained.
    amazon_max_requests_per_minute: float = 20.0
    # After Amazon throttles / refuses a run, later jobs within this many
    # minutes stop immediately instead of hitting the block again (0 = off).
    amazon_cooldown_minutes: float = 60.0

    # Incremental storage (all platforms that emit products as they finish):
    # records / products / retailers / reviews are bulk-upserted in batches
    # while the run continues, and progress is checkpointed for resume.
    storage_batch_size: int = 500
    # Processes for review language detection on large batches (None = CPUs-1, max 4; 0 = in-process).
    language_workers: int | None = None
    resume_completed_within_hours: float = 24.0

    # Alibaba engine: navigation retries with backoff and a polite randomized
    # gap between navigations shared across workers (as Amazon).
    alibaba_max_retries: int = 3
    alibaba_min_delay_seconds: float = 1.2
    alibaba_max_delay_seconds: float = 2.5

    debug_dir: str = str(PROJECT_ROOT / "local_data" / "debug")

    export_dir: str = str(PROJECT_ROOT / "local_data" / "exports")
    export_json: bool = True
    export_excel: bool = True

    collection_authorized: bool = False
    authorization_reference: str | None = None

    # ── Reporting engine ────────────────────────────────────────────────────
    # Where generated report artifacts (pdf/xlsx) are written.
    report_export_dir: str = str(PROJECT_ROOT / "local_data" / "reports")
    # Reuse an existing report_summaries document when its filters, aggregation
    # version and data snapshot signature all match the new request.
    report_reuse_summaries: bool = True
    # Max representative reviews attached as evidence per section sent to the LLM.
    report_evidence_per_section: int = 6
    # Risk-rule thresholds (configurable; the report states the calculation).
    risk_growth_qoq_threshold: float = 20.0      # % QoQ volume growth
    risk_negative_share_threshold: float = 55.0  # % negative share
    risk_min_volume: int = 10                    # ignore themes below this volume

    # ── LLM providers (provider-independent; none required to run) ──────────
    # One of: "fallback" (deterministic, no network), "ollama", "gemini",
    # "openrouter". An unconfigured/unreachable provider degrades to "fallback".
    llm_provider: str = "fallback"
    llm_timeout: int = 120
    llm_max_retries: int = 3
    llm_temperature: float = 0.2

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    openrouter_api_key: str | None = None
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    def profile_for(self, platform: str) -> str:
        return str(Path(self.profile_base_dir) / platform)

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
