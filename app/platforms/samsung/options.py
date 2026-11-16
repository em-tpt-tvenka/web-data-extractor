from __future__ import annotations

from pydantic import BaseModel

from app.schemas.config_schema import ConfigField, FieldType, PlatformConfigSchema


class SamsungOptions(BaseModel):
    category: str | None = None

    model_config = {"extra": "forbid"}


def config_schema() -> PlatformConfigSchema:
    fields = [
        ConfigField(
            name="category",
            label="Category override",
            type=FieldType.string,
            default=None,
            description="Force a Samsung catalog category (smartphones, laptops, tvs, tablets, "
                        "watches, refrigerators, monitors, audio-devices); auto-detected from the "
                        "search term when omitted.",
        ),
    ]
    return PlatformConfigSchema(platform="samsung", fields=fields)
