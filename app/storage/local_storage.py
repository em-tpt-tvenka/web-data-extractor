"""Local filesystem storage for scraped records.

Writes each scrape run to an isolated, per-platform folder as both a JSON file
(full-fidelity, nested) and an Excel workbook (flattened, one row per record).

Layout::

    local_data/exports/
        alibaba/
            alibaba_<run_id>_<timestamp>.json
            alibaba_<run_id>_<timestamp>.xlsx
        amazon/
            ...

The JSON preserves the exact record structure that is stored in MongoDB. The
Excel workbook has one column per record field (fixed, derived from the record
schema so it never gains or drops columns between runs): nested fixed blocks are
flattened to dotted columns (e.g. ``commercial.price.selling_price.amount``),
while list fields and the free-form ``specifications`` dict are written as a
single compact-JSON cell instead of being exploded into many columns.
"""

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
from app.schemas.record import SocialMentionRecord

_log = get_logger("local_storage")

# Excel hard-caps a single cell at 32767 characters.
_EXCEL_CELL_LIMIT = 32_767


def _safe(part: str) -> str:
    """Make a path-safe fragment from a platform name or run id."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(part or "unknown")).strip("_") or "unknown"


def _unwrap_optional(annotation: Any) -> Any:
    """Return the inner type of Optional[X] / ``X | None``; otherwise annotation."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _schema_columns(model_cls: type[BaseModel], prefix: str = "") -> list[str]:
    """Build the fixed, ordered column list for a record model.

    Fixed sub-models are recursed into (dotted columns); lists and dicts become a
    single column (serialized as JSON in the cell); scalars are a single column.
    """
    columns: list[str] = []
    for name, field in model_cls.model_fields.items():
        key = field.alias or name  # top-level id -> "_id"
        col = f"{prefix}.{key}" if prefix else key
        inner = _unwrap_optional(field.annotation)
        if _is_model(inner):
            columns.extend(_schema_columns(inner, col))
        else:
            columns.append(col)  # scalar, list, or dict -> one column
    return columns


# Computed once: the canonical Excel column order for every exported record.
_RECORD_COLUMNS: list[str] = _schema_columns(SocialMentionRecord)


def _get_by_path(data: dict, dotted: str) -> Any:
    """Walk a dotted path through nested dicts. Returns None if any step is
    missing or a parent block is null."""
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _excel_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        text = value
    else:
        # Lists / dicts (e.g. specifications, brands) -> compact JSON string.
        text = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(text, str) and len(text) > _EXCEL_CELL_LIMIT:
        text = text[: _EXCEL_CELL_LIMIT - 1] + "…"
    return text


class LocalStorage:
    """Writes scraped records to per-platform JSON and Excel files on disk."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir or settings.export_dir)

    def platform_dir(self, platform: str) -> Path:
        return self.base_dir / _safe(platform)

    def export_records(
        self,
        platform: str,
        run_id: str,
        records: list[SocialMentionRecord],
    ) -> dict[str, str]:
        """Write records for one run. Returns a map of written file paths
        (keys: ``json`` and/or ``excel``). Export failures are logged and do not
        raise, so they never break the scraping pipeline."""
        paths: dict[str, str] = {}
        if not (settings.export_json or settings.export_excel):
            return paths

        target = self.platform_dir(platform)
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        stem = f"{_safe(platform)}_{_safe(run_id)}_{stamp}"

        dicts = [r.to_mongo() for r in records]

        if settings.export_json:
            try:
                paths["json"] = self._write_json(target / f"{stem}.json", dicts)
            except Exception as exc:  # pragma: no cover - defensive
                _log.error("event=export_json_failed platform=%s run_id=%s error=%s",
                           platform, run_id, exc)

        if settings.export_excel:
            try:
                paths["excel"] = self._write_excel(target / f"{stem}.xlsx", dicts)
            except Exception as exc:  # pragma: no cover - defensive
                _log.error("event=export_excel_failed platform=%s run_id=%s error=%s",
                           platform, run_id, exc)

        if paths:
            _log.info("event=export_done platform=%s run_id=%s records=%d files=%s",
                      platform, run_id, len(records), list(paths))
        return paths

    @staticmethod
    def _write_json(path: Path, dicts: list[dict]) -> str:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(dicts, fh, ensure_ascii=False, indent=2, default=str)
        return str(path)

    @staticmethod
    def _write_excel(path: Path, dicts: list[dict]) -> str:
        columns = _RECORD_COLUMNS
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title="records")
        ws.append(columns)
        for data in dicts:
            ws.append([_excel_cell(_get_by_path(data, col)) for col in columns])
        wb.save(path)
        return str(path)
