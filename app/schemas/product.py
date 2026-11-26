from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

class BrandEntity(BaseModel):
    model_config = ConfigDict(extra="allow")

    brand_id: str | None = None
    brand_name: str | None = None
    brand_role: str | None = None

class ProductEntity(BaseModel):
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    product_id: str | None = None
    product_name: str | None = None
    sku: str | None = None
    model_number: str | None = None
    product_url: str | None = None
    # Cross-platform grouping keys (see app.platforms.common.canonical_ids):
    # canonical_product_id is stable for the same model LINE regardless of
    # configuration or platform (e.g. "Galaxy A36 5G" on any site/any variant);
    # variant_key additionally pins the specific storage/color configuration,
    # so a price comparison can tell "same phone, different RAM/color" (not
    # comparable) apart from "same phone, same configuration" (comparable).
    canonical_product_id: str | None = None
    variant_key: str | None = None

class RetailerEntity(BaseModel):
    model_config = ConfigDict(extra="allow")

    retailer_id: str | None = None
    retailer_name: str | None = None

class EntitiesBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    brands: list[BrandEntity] = Field(default_factory=list)
    products: list[ProductEntity] = Field(default_factory=list)
    retailer: RetailerEntity | None = None

class Capacity(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: float | None = None
    unit: str | None = None

class ProductAttributesBlock(BaseModel):
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model_number: str | None = None
    # The marketing/model-LINE name (e.g. "Galaxy S26 5G"), distinct from
    # model_number (e.g. "SM-S938B") - kept as its own field so the two are
    # never conflated (a real product's "Model Name" spec row was previously
    # mistakenly surfaced as model_number on Flipkart).
    model_name: str | None = None
    capacity: Capacity | None = None
    color: str | None = None
    energy_rating: str | None = None
    highlights: list[str] = Field(default_factory=list)
    variants: list[Any] | dict[str, Any] = Field(default_factory=list)
    manufacturer: dict[str, Any] | None = None
    specifications: dict[str, Any] = Field(default_factory=dict)
    # Independently-scraped variant records (own price/specs/images/availability
    # per SKU) - additive, separate from `variants` above (which stays the
    # original swatch-label metadata shape so existing consumers of that field
    # are unaffected). Each entry is a free-form dict shaped like:
    # {variant_id, parent_product_id, platform_variant_id, variant_name,
    #  attributes, product_url, images, specifications, availability,
    #  commercial, metrics, variant_extraction}.
    variant_details: list[dict[str, Any]] = Field(default_factory=list)

class TaxonomyBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    industry: str | None = None
    category: str | None = None
    subcategory: str | None = None
    product_type: str | None = None
    taxonomy_version: str | None = None
    mapping_method: str | None = None
    mapping_status: str = "unmapped"
