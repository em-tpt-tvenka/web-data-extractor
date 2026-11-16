from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

class FieldType(str, Enum):
    boolean = "boolean"
    integer = "integer"
    decimal = "decimal"
    string = "string"
    enum = "enum"
    array = "array"

class FieldOption(BaseModel):
    value: str
    label: str

class ValidationRule(BaseModel):
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None

class ConfigField(BaseModel):
    name: str
    label: str
    type: FieldType
    default: Any = None
    description: str | None = None
    options: list[FieldOption] = Field(default_factory=list)
    item_type: FieldType | None = None
    validation: ValidationRule = Field(default_factory=ValidationRule)

class PlatformConfigSchema(BaseModel):
    platform: str
    fields: list[ConfigField]
