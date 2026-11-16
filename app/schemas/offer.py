from __future__ import annotations

from pydantic import BaseModel

class MoneyAmount(BaseModel):
    amount: float | None = None
    currency: str | None = None

class PriceBlock(BaseModel):
    listed_price: MoneyAmount | None = None
    selling_price: MoneyAmount | None = None
    original_price: MoneyAmount | None = None
    discount_amount: float | None = None
    discount_percentage: float | None = None
    price_type: str | None = None
    price_observed_at: str | None = None

class PriceRangeBlock(BaseModel):
    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None

class PromotionBlock(BaseModel):
    is_promotional: bool = False
    promotion_type: str | None = None
    promotion_name: str | None = None
    coupon_available: bool | None = None
    coupon_code: str | None = None

class PaymentOptionsBlock(BaseModel):
    emi_available: bool | None = None
    no_cost_emi: bool | None = None
    bank_offer_available: bool | None = None
    exchange_offer_available: bool | None = None
    cash_on_delivery: bool | None = None

class AvailabilityBlock(BaseModel):
    status: str | None = None
    stock_quantity: int | None = None

class DeliveryBlock(BaseModel):
    delivery_charge: MoneyAmount | None = None
    estimated_delivery_date: str | None = None
    delivery_type: str | None = None

class WarrantyBlock(BaseModel):
    duration_months: int | None = None
    warranty_type: str | None = None

class CommercialBlock(BaseModel):
    price: PriceBlock | None = None
    price_range: PriceRangeBlock | None = None
    promotion: PromotionBlock | None = None
    payment_options: PaymentOptionsBlock | None = None
    availability: AvailabilityBlock | None = None
    delivery: DeliveryBlock | None = None
    warranty: WarrantyBlock | None = None
