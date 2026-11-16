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
    chrome_profile_dir: str | None = str(PROJECT_ROOT / "local_data" / "chrome_scraper_profile")
    nav_timeout_ms: int = 90_000
    review_concurrency: int = 3
    feedback_concurrency: int = 2
    feedback_max_pages: int = 80
    captcha_wait_seconds: int = 300

    samsung_base_url: str = "https://www.samsung.com/in"
    samsung_bv_api_url: str | None = None
    samsung_bv_passkey: str | None = None

    raw_data_dir: str = str(PROJECT_ROOT / "local_data" / "raw")
    debug_dir: str = str(PROJECT_ROOT / "local_data" / "debug")

    export_dir: str = str(PROJECT_ROOT / "local_data" / "exports")
    export_json: bool = True
    export_excel: bool = True

    collection_authorized: bool = False
    authorization_reference: str | None = None

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
