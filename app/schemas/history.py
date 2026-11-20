from __future__ import annotations

from pydantic import BaseModel, ConfigDict

class LifecycleBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    first_seen_at: str | None = None
    last_seen_at: str | None = None
    record_status: str = "active"
