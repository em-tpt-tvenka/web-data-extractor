from __future__ import annotations

from app.processing import deduplicator, validator
from app.schemas.record import SocialMentionRecord

def finalize(records: list[SocialMentionRecord]) -> list[SocialMentionRecord]:
    deduplicator.apply_deduplication(records)
    validator.validate_batch(records)
    return records
