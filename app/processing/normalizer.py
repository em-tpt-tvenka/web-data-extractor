from __future__ import annotations

from app.processing import deduplicator, validator
from app.schemas.record import SocialMentionRecord


def finalize(records: list[SocialMentionRecord]) -> list[SocialMentionRecord]:
    """Shared post-mapping pipeline: cross-record deduplication and per-record
    schema validation/relevance scoring. Platform-specific field mapping happens
    in the adapter; this stage is platform-agnostic."""
    deduplicator.apply_deduplication(records)
    validator.validate_batch(records)
    return records
