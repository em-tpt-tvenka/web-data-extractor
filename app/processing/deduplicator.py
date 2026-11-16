from __future__ import annotations

from app.schemas.record import SocialMentionRecord

def apply_deduplication(records: list[SocialMentionRecord]) -> list[SocialMentionRecord]:
    canonical_by_hash: dict[str, str] = {}
    for record in records:
        content_hash = record.deduplication.content_hash
        if not content_hash:
            record.deduplication.duplicate_status = "unique"
            record.deduplication.canonical_record_id = record.id
            continue
        canonical = canonical_by_hash.get(content_hash)
        if canonical is None:
            canonical_by_hash[content_hash] = record.id
            record.deduplication.duplicate_status = "unique"
            record.deduplication.canonical_record_id = record.id
            record.deduplication.duplicate_of = None
        else:
            record.deduplication.duplicate_status = "duplicate"
            record.deduplication.canonical_record_id = canonical
            record.deduplication.duplicate_of = canonical
    return records
