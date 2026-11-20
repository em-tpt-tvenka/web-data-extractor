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
    capacity: Capacity | None = None
    color: str | None = None
    energy_rating: str | None = None
    highlights: list[str] = Field(default_factory=list)
    variants: list[Any] | dict[str, Any] = Field(default_factory=list)
    manufacturer: dict[str, Any] | None = None
    specifications: dict[str, Any] = Field(default_factory=dict)

class TaxonomyBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    industry: str | None = None
    category: str | None = None
    subcategory: str | None = None
    product_type: str | None = None
    taxonomy_version: str | None = None
    mapping_method: str | None = None
    mapping_status: str = "unmapped"
