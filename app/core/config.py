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

    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

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
    amazon_detail_concurrency: int = 1
    amazon_max_retries: int = 3
    amazon_min_delay_seconds: float = 2.0
    amazon_max_delay_seconds: float = 4.5

    debug_dir: str = str(PROJECT_ROOT / "local_data" / "debug")

    export_dir: str = str(PROJECT_ROOT / "local_data" / "exports")
    export_json: bool = True
    export_excel: bool = True

    collection_authorized: bool = False
    authorization_reference: str | None = None

    def profile_for(self, platform: str) -> str:
        return str(Path(self.profile_base_dir) / platform)

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
