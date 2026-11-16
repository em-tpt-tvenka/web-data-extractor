from __future__ import annotations

from collections import Counter

from app.schemas.record import SocialMentionRecord

def summarize(records: list[SocialMentionRecord]) -> dict:
    status = Counter(r.quality.status for r in records)
    dup = Counter(r.deduplication.duplicate_status for r in records)
    return {
        "total": len(records),
        "accepted": status.get("accepted", 0),
        "rejected": status.get("rejected", 0),
        "irrelevant": status.get("irrelevant", 0),
        "unique": dup.get("unique", 0),
        "duplicate": dup.get("duplicate", 0),
    }
