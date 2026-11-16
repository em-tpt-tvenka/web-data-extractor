from __future__ import annotations

from pydantic import BaseModel

class LifecycleBlock(BaseModel):
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    record_status: str = "active"
