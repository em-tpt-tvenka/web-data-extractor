from __future__ import annotations

from app.platforms.categories import LISTING_RECORD_TYPES
from app.schemas.record import SocialMentionRecord

_REQUIRED = {
    "_id": lambda r: bool(r.id),
    "tenant.tenant_id": lambda r: bool(r.tenant.tenant_id),
    "source.platform": lambda r: bool(r.source.platform),
    "source.source_item_id": lambda r: bool(r.source.source_item_id),
}

_OPTIONAL_CHECKS = {
    "content.text": lambda r: bool(r.content.text),
    "source.published_at": lambda r: bool(r.source.published_at),
    "metrics.rating": lambda r: r.metrics.rating is not None and r.metrics.rating.value is not None,
    "author.display_name": lambda r: bool(r.author.display_name),
    "entities.brands": lambda r: bool(r.entities.brands),
    "commercial.price.selling_price": lambda r: bool(
        r.commercial.price and r.commercial.price.selling_price
        and r.commercial.price.selling_price.amount is not None
    ),
    "product_attributes.model_number": lambda r: bool(r.product_attributes.model_number),
}


def _is_relevant(record: SocialMentionRecord) -> bool:
    if record.record_type in LISTING_RECORD_TYPES:
        return bool(record.content.text or record.entities.products)
    # A review/mention must carry actual textual content to be accepted; a rating
    # with empty/whitespace-only text is not a valid review.
    return bool(record.content.text and record.content.text.strip())


def validate_record(record: SocialMentionRecord) -> SocialMentionRecord:
    errors = [f"missing required field: {name}" for name, check in _REQUIRED.items() if not check(record)]
    missing_optional = [name for name, check in _OPTIONAL_CHECKS.items() if not check(record)]

    relevant = _is_relevant(record)
    valid = not errors

    record.quality.validation_errors = errors
    record.quality.missing_optional_fields = missing_optional
    record.quality.is_valid = valid
    record.quality.is_relevant = relevant
    if not valid:
        record.quality.status = "rejected"
    elif not relevant:
        record.quality.status = "irrelevant"
    else:
        record.quality.status = "accepted"
    return record


def validate_batch(records: list[SocialMentionRecord]) -> list[SocialMentionRecord]:
    return [validate_record(r) for r in records]
