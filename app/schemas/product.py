from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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

class Capacity(BaseModel):
    value: float | None = None
    unit: str | None = None

class ProductAttributesBlock(BaseModel):
    model_number: str | None = None
    capacity: Capacity | None = None
    color: str | None = None
    energy_rating: str | None = None
    specifications: dict[str, Any] = Field(default_factory=dict)

class TaxonomyBlock(BaseModel):
    industry: str | None = None
    category: str | None = None
    subcategory: str | None = None
    product_type: str | None = None
    taxonomy_version: str | None = None
    mapping_method: str | None = None
    mapping_status: str = "unmapped"
