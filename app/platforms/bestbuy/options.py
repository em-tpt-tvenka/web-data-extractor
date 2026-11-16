from __future__ import annotations

from pydantic import BaseModel

from app.schemas.config_schema import PlatformConfigSchema


class BestBuyOptions(BaseModel):
    model_config = {"extra": "forbid"}


def config_schema() -> PlatformConfigSchema:
    return PlatformConfigSchema(platform="bestbuy", fields=[])
