from __future__ import annotations

import json
import re
import types
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger
from app.processing.collection_splitter import split_records
from app.schemas.record import SocialMentionRecord

_log = get_logger("local_storage")

_EXCEL_CELL_LIMIT = 32_767

def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(part or "unknown")).strip("_") or "unknown"


def _slug(text: str | None, max_len: int = 40) -> str:
    """Short, lowercase, filename-friendly label, e.g. 'Stainless Steel Bottle'
    -> 'stainless-steel-bottle'. Empty string when there is nothing usable."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-")


def _unwrap_optional(annotation: Any) -> Any:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation

def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)

def _schema_columns(model_cls: type[BaseModel], prefix: str = "") -> list[str]:
    columns: list[str] = []
    for name, field in model_cls.model_fields.items():
        key = field.alias or name
        col = f"{prefix}.{key}" if prefix else key
        inner = _unwrap_optional(field.annotation)
        if _is_model(inner):
            columns.extend(_schema_columns(inner, col))
        else:
            columns.append(col)
    return columns


# Columns a reader scans first, shown at the left of the sheet. Every other
# schema column follows in schema order, so no data is dropped.
_PRIORITY_COLUMNS: list[str] = [
    "record_type",
    "source.source_type",
    "content.title",
    "entities.brands",
    "product_attributes.model_number",
    "commercial.price.selling_price.amount",
    "commercial.price.selling_price.currency",
    "commercial.price.original_price.amount",
    "commercial.price.discount_percentage",
    "metrics.rating.value",
    "metrics.review_count",
    "commercial.availability.status",
    "author.display_name",
    "content.text",
    "source.source_url",
    "source.collected_at",
]


def _ordered_columns(schema_columns: list[str]) -> list[str]:
    front = [c for c in _PRIORITY_COLUMNS if c in schema_columns]
    return front + [c for c in schema_columns if c not in front]


# Computed once: the canonical Excel column order for every exported record.
_RECORD_COLUMNS: list[str] = _ordered_columns(_schema_columns(SocialMentionRecord))

def _get_by_path(data: dict, dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _product_name(data: dict) -> str | None:
    """Name of the product a record is about. For reviews ``content.title`` is
    the review headline, so read it from ``entities.products`` instead."""
    for product in _get_by_path(data, "entities.products") or []:
        if isinstance(product, dict) and product.get("product_name"):
            return product["product_name"]
    return None


def _excel_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(text, str) and len(text) > _EXCEL_CELL_LIMIT:
        text = text[: _EXCEL_CELL_LIMIT - 1] + "…"
    return text

class LocalStorage:
    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir or settings.export_dir)

    def platform_dir(self, platform: str) -> Path:
        return self.base_dir / _safe(platform)

    def export_records(
        self,
        platform: str,
        run_id: str,
        records: list[SocialMentionRecord],
        label: str | None = None,
    ) -> dict[str, str]:
        paths: dict[str, str] = {}
        if not (settings.export_json or settings.export_excel):
            return paths

        target = self.platform_dir(platform)
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # Timestamp first so sorting by name == sorting by time (newest last);
        # then the searched product for readability; job id keeps it unique.
        parts = [_safe(platform), stamp, _slug(label), _safe(run_id)]
        stem = "_".join(p for p in parts if p)

        dicts = [r.to_mongo() for r in records]

        if settings.export_json:
            # A single JSON file per job holding the three collections exactly as
            # stored in MongoDB, keyed by collection:
            # {"products": [...], "retailers": [...], "reviews": [...]}.
            # The DB still writes each collection to its own table; only this
            # on-disk export is combined into one file.
            try:
                paths["json"] = self._write_json(
                    target / f"{stem}.json", split_records(dicts))
            except Exception as exc:
                _log.error("event=export_collections_failed platform=%s run_id=%s error=%s",
                           platform, run_id, exc)

        if settings.export_excel:
            try:
                paths["excel"] = self._write_excel(target / f"{stem}.xlsx", dicts)
            except Exception as exc:
                _log.error("event=export_excel_failed platform=%s run_id=%s error=%s",
                           platform, run_id, exc)

        if paths:
            _log.info("event=export_done platform=%s run_id=%s records=%d files=%s",
                      platform, run_id, len(records), list(paths))
        return paths

    @staticmethod
    def _write_json(path: Path, payload: Any) -> str:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        return str(path)

    @staticmethod
    def _write_excel(path: Path, dicts: list[dict]) -> str:
        columns = _RECORD_COLUMNS
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title="records")
        ws.freeze_panes = "A2"  # keep the header row visible while scrolling
        # Excel-only convenience column first; the rest mirror the record schema.
        ws.append(["product_name", *columns])
        for data in dicts:
            ws.append([
                _excel_cell(_product_name(data)),
                *(_excel_cell(_get_by_path(data, col)) for col in columns),
            ])
        wb.save(path)
        return str(path)
