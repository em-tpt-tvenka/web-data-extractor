from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class MoneyAmount(BaseModel):
    model_config = ConfigDict(extra="allow")

    amount: float | None = None
    currency: str | None = None

class PriceBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    listed_price: MoneyAmount | None = None
    selling_price: MoneyAmount | None = None
    original_price: MoneyAmount | None = None
    discount_amount: float | None = None
    discount_percentage: float | None = None
    price_type: str | None = None
    price_observed_at: str | None = None
    lowest_price_with_offers: MoneyAmount | None = None
    offer_discount_amount: float | None = None
    extra_discount_amount: float | None = None

class PriceRangeBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None

class PromotionBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    is_promotional: bool = False
    promotion_type: str | None = None
    promotion_name: str | None = None
    coupon_available: bool | None = None
    coupon_code: str | None = None

class PaymentOptionsBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    emi_available: bool | None = None
    no_cost_emi: bool | None = None
    bank_offer_available: bool | None = None
    exchange_offer_available: bool | None = None
    cash_on_delivery: bool | None = None

class AvailabilityBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    stock_quantity: int | None = None

class DeliveryBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    delivery_charge: MoneyAmount | None = None
    estimated_delivery_date: str | None = None
    delivery_type: str | None = None

class WarrantyBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    duration_months: int | None = None
    warranty_type: str | None = None

class CommercialBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    price: PriceBlock | None = None
    price_range: PriceRangeBlock | None = None
    promotion: PromotionBlock | None = None
    payment_options: PaymentOptionsBlock | None = None
    availability: AvailabilityBlock | None = None
    delivery: DeliveryBlock | None = None
    warranty: WarrantyBlock | None = None
    offers_detail: dict[str, Any] | None = None
