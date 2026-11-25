"""Index definitions for the reporting collections, created idempotently."""

from __future__ import annotations

from pymongo import ASCENDING, DESCENDING

from app.core.logging import get_logger
from app.reporting.repositories import (
    REPORT_ARTIFACTS,
    REPORT_INSIGHTS,
    REPORT_SUMMARIES,
    REPORTS,
)
from app.storage.database import get_db

_log = get_logger("reporting.indexes")

_INDEXES = {
    REPORT_SUMMARIES: [
        ([("summary_key", ASCENDING)], {"name": "summary_key"}),
        ([("tenant_id", ASCENDING), ("project_id", ASCENDING)], {"name": "tenant_project"}),
        ([("summary_type", ASCENDING)], {"name": "summary_type"}),
        ([("created_at", DESCENDING)], {"name": "created_at"}),
    ],
    REPORTS: [
        ([("_id", ASCENDING)], {"name": "_id_", "existing": True}),
        ([("tenant_id", ASCENDING), ("project_id", ASCENDING)], {"name": "tenant_project"}),
        ([("status", ASCENDING)], {"name": "status"}),
        ([("summary_id", ASCENDING)], {"name": "summary_id"}),
        ([("created_at", DESCENDING)], {"name": "created_at"}),
    ],
    REPORT_INSIGHTS: [
        ([("report_id", ASCENDING)], {"name": "report_id"}),
        ([("summary_id", ASCENDING)], {"name": "summary_id"}),
        ([("tenant_id", ASCENDING), ("project_id", ASCENDING)], {"name": "tenant_project"}),
        ([("section", ASCENDING)], {"name": "section"}),
    ],
    REPORT_ARTIFACTS: [
        ([("report_id", ASCENDING)], {"name": "report_id"}),
        ([("tenant_id", ASCENDING), ("project_id", ASCENDING)], {"name": "tenant_project"}),
    ],
}

_done: set[int] = set()


async def ensure_reporting_indexes() -> None:
    db = get_db()
    key = id(db.client)
    if key in _done:
        return
    for collection, specs in _INDEXES.items():
        for keys, opts in specs:
            if opts.pop("existing", False):
                continue
            try:
                await db[collection].create_index(keys, **opts)
            except Exception as exc:  # pragma: no cover - index race/opts
                _log.warning("event=index_failed collection=%s error=%s", collection, exc)
    _done.add(key)
