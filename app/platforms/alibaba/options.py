from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.schemas.config_schema import (
    ConfigField,
    FieldOption,
    FieldType,
    PlatformConfigSchema,
    ValidationRule,
)

class SupplierType(str, Enum):
    manufacturer = "manufacturer"
    trading_company = "trading_company"
    all = "all"

class AlibabaOptions(BaseModel):
    verified_supplier_only: bool = False
    trade_assurance_only: bool = False
    min_order_quantity: int | None = Field(default=None, ge=1)
    max_order_quantity: int | None = Field(default=None, ge=1)
    supplier_country: list[str] = Field(default_factory=list)
    supplier_type: SupplierType = SupplierType.all
    include_sponsored: bool = False
    verified_reviews_only: bool = False

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _moq_order(self) -> AlibabaOptions:
        if (
            self.min_order_quantity is not None
            and self.max_order_quantity is not None
            and self.min_order_quantity > self.max_order_quantity
        ):
            raise ValueError("min_order_quantity cannot exceed max_order_quantity")
        return self

def config_schema() -> PlatformConfigSchema:
    fields = [
        ConfigField(
            name="verified_supplier_only",
            label="Verified supplier only",
            type=FieldType.boolean,
            default=False,
            description="Restrict results to verified suppliers.",
        ),
        ConfigField(
            name="trade_assurance_only",
            label="Trade Assurance only",
            type=FieldType.boolean,
            default=False,
            description="Restrict results to Trade Assurance suppliers.",
        ),
        ConfigField(
            name="min_order_quantity",
            label="Minimum order quantity",
            type=FieldType.integer,
            default=None,
            description="Minimum MOQ to include.",
            validation=ValidationRule(minimum=1),
        ),
        ConfigField(
            name="max_order_quantity",
            label="Maximum order quantity",
            type=FieldType.integer,
            default=None,
            description="Maximum MOQ to include.",
            validation=ValidationRule(minimum=1),
        ),
        ConfigField(
            name="supplier_country",
            label="Supplier country",
            type=FieldType.array,
            item_type=FieldType.string,
            default=[],
            description="Filter suppliers by registered country.",
        ),
        ConfigField(
            name="supplier_type",
            label="Supplier type",
            type=FieldType.enum,
            default=SupplierType.all.value,
            description="Manufacturer, trading company, or all.",
            options=[
                FieldOption(value=SupplierType.manufacturer.value, label="Manufacturer"),
                FieldOption(value=SupplierType.trading_company.value, label="Trading Company"),
                FieldOption(value=SupplierType.all.value, label="All"),
            ],
        ),
        ConfigField(
            name="include_sponsored",
            label="Include sponsored listings",
            type=FieldType.boolean,
            default=False,
            description="Include sponsored/ad listings in results.",
        ),
        ConfigField(
            name="verified_reviews_only",
            label="Verified purchase reviews only",
            type=FieldType.boolean,
            default=False,
            description="Collect only reviews and supplier feedback marked 'Verified Purchase'.",
        ),
    ]
    return PlatformConfigSchema(platform="alibaba", fields=fields)
