from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.history import LifecycleBlock
from app.schemas.offer import (
    AvailabilityBlock,
    CommercialBlock,
    DeliveryBlock,
    MoneyAmount,
    PaymentOptionsBlock,
    PriceBlock,
    PriceRangeBlock,
    PromotionBlock,
    WarrantyBlock,
)
from app.schemas.product import (
    BrandEntity,
    Capacity,
    EntitiesBlock,
    ProductAttributesBlock,
    ProductEntity,
    RetailerEntity,
    TaxonomyBlock,
)
from app.schemas.review import (
    AuthorBlock,
    ContentBlock,
    EngagementMetric,
    LocationBlock,
    MediaItem,
    MetricsBlock,
    RatingMetric,
)

SCHEMA_VERSION = "1.0"

class TenantBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    tenant_id: str
    project_id: str | None = None
    report_scope_ids: list[str] = Field(default_factory=list)

class SourceBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

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

class CampaignBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    campaign_name: str | None = None
    campaign_type: str | None = None
    event_name: str | None = None
    campaign_id: str | None = None

class DeduplicationBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    content_hash: str | None = None
    duplicate_status: str = "unique"
    canonical_record_id: str | None = None
    duplicate_of: str | None = None

class ProvenanceBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    collection_run_id: str | None = None
    connector_name: str | None = None
    connector_version: str | None = None
    normalizer_version: str | None = None
    source_payload_ref: str | None = None
    ingestion_timestamp: str | None = None

class QualityBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "accepted"
    is_valid: bool = True
    is_relevant: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    missing_optional_fields: list[str] = Field(default_factory=list)

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
    location: LocationBlock | None = None
    campaign: CampaignBlock
    deduplication: DeduplicationBlock
    provenance: ProvenanceBlock
    quality: QualityBlock
    lifecycle: LifecycleBlock

    # Multi-platform product scrape extensions (reviews, review summary)
    review_summary: dict[str, Any] | None = None
    reviews: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def to_mongo(self) -> dict[str, Any]:
        d = self.model_dump(by_alias=True)
        if d.get("location") is None:
            d.pop("location", None)
        return d

__all__ = [
    "SCHEMA_VERSION",
    "MoneyAmount",
    "PriceBlock",
    "PriceRangeBlock",
    "PromotionBlock",
    "PaymentOptionsBlock",
    "AvailabilityBlock",
    "DeliveryBlock",
    "WarrantyBlock",
    "CommercialBlock",
    "BrandEntity",
    "ProductEntity",
    "RetailerEntity",
    "EntitiesBlock",
    "Capacity",
    "ProductAttributesBlock",
    "TaxonomyBlock",
    "MediaItem",
    "ContentBlock",
    "RatingMetric",
    "EngagementMetric",
    "MetricsBlock",
    "AuthorBlock",
    "LocationBlock",
    "LifecycleBlock",
    "TenantBlock",
    "SourceBlock",
    "CampaignBlock",
    "DeduplicationBlock",
    "ProvenanceBlock",
    "QualityBlock",
    "SocialMentionRecord",
]
