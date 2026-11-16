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

_EXCEL_CELL_LIMIT = 32_767

def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(part or "unknown")).strip("_") or "unknown"

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

_RECORD_COLUMNS: list[str] = _schema_columns(SocialMentionRecord)

def _get_by_path(data: dict, dotted: str) -> Any:
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
    ) -> dict[str, str]:
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
            except Exception as exc:
                _log.error("event=export_json_failed platform=%s run_id=%s error=%s",
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
