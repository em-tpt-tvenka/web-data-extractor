from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class MoneyAmount(BaseModel):
    amount: float | None = None
    currency: str | None = None


class TenantBlock(BaseModel):
    tenant_id: str
    project_id: str | None = None
    report_scope_ids: list[str] = Field(default_factory=list)


class SourceBlock(BaseModel):
    platform: str
    channel: str | None = None
    source_type: str | None = None
    source_item_id: str
    source_url: str | None = None
    parent_item_id: str | None = None
    published_at: str | None = None
    collected_at: str | None = None
    updated_at: str | None = None
    country_code: str | None = None
    marketplace: str | None = None


class MediaItem(BaseModel):
    media_type: str
    url: str


class ContentBlock(BaseModel):
    title: str | None = None
    text: str | None = None
    language: str | None = None
    original_language: str | None = None
    content_type: str | None = None
    media: list[MediaItem] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class BrandEntity(BaseModel):
    brand_id: str | None = None
    brand_name: str | None = None
    brand_role: str | None = None


class ProductEntity(BaseModel):
    product_id: str | None = None
    product_name: str | None = None
    sku: str | None = None
    model_number: str | None = None
    product_url: str | None = None


class RetailerEntity(BaseModel):
    retailer_id: str | None = None
    retailer_name: str | None = None


class EntitiesBlock(BaseModel):
    brands: list[BrandEntity] = Field(default_factory=list)
    products: list[ProductEntity] = Field(default_factory=list)
    retailer: RetailerEntity | None = None
    other_brands: list[BrandEntity] = Field(default_factory=list)


class TaxonomyBlock(BaseModel):
    industry: str | None = None
    category: str | None = None
    subcategory: str | None = None
    product_type: str | None = None
    taxonomy_version: str | None = None
    mapping_method: str | None = None
    mapping_status: str = "unmapped"


class Capacity(BaseModel):
    value: float | None = None
    unit: str | None = None


class ProductAttributesBlock(BaseModel):
    model_number: str | None = None
    capacity: Capacity | None = None
    color: str | None = None
    energy_rating: str | None = None
    specifications: dict[str, Any] = Field(default_factory=dict)


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


class RatingMetric(BaseModel):
    value: float | None = None
    scale_min: int = 1
    scale_max: int = 5


class EngagementMetric(BaseModel):
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    reactions: int | None = None


class MetricsBlock(BaseModel):
    rating: RatingMetric | None = None
    engagement: EngagementMetric = Field(default_factory=EngagementMetric)
    helpful_votes: int | None = None
    review_count: int | None = None
    author_followers: int | None = None
    reach: int | None = None


class AuthorBlock(BaseModel):
    source_author_id_hash: str | None = None
    display_name: str | None = None
    author_type: str | None = None
    is_verified_purchase: bool | None = None
    profile_url: str | None = None
    follower_count: int | None = None


class LocationBlock(BaseModel):
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    location_precision: str | None = None


class CampaignBlock(BaseModel):
    campaign_name: str | None = None
    campaign_type: str | None = None
    event_name: str | None = None
    campaign_id: str | None = None


class DeduplicationBlock(BaseModel):
    content_hash: str | None = None
    duplicate_status: str = "unique"
    canonical_record_id: str | None = None
    duplicate_of: str | None = None


class ProvenanceBlock(BaseModel):
    collection_run_id: str | None = None
    connector_name: str | None = None
    connector_version: str | None = None
    normalizer_version: str | None = None
    source_payload_ref: str | None = None
    ingestion_timestamp: str | None = None


class QualityBlock(BaseModel):
    status: str = "accepted"
    is_valid: bool = True
    is_relevant: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    missing_optional_fields: list[str] = Field(default_factory=list)


class LifecycleBlock(BaseModel):
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    record_status: str = "active"


class SocialMentionRecord(BaseModel):
    id: str = Field(alias="_id")
    schema_version: str = SCHEMA_VERSION
    record_type: str = "social_mention"
    tenant: TenantBlock
    source: SourceBlock
    content: ContentBlock
    entities: EntitiesBlock
    taxonomy: TaxonomyBlock
    product_attributes: ProductAttributesBlock
    commercial: CommercialBlock
    metrics: MetricsBlock
    author: AuthorBlock
    location: LocationBlock
    campaign: CampaignBlock
    deduplication: DeduplicationBlock
    provenance: ProvenanceBlock
    quality: QualityBlock
    lifecycle: LifecycleBlock

    model_config = {"populate_by_name": True}

    def to_mongo(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)
