"""Record-type and source-classification constants shared across platforms.

A collected item is a social-mention record (customer review or supplier
feedback) or a product-listing fallback. The specific nature of a mention is
carried by source_type / channel / content_type, not by record_type.
"""

from __future__ import annotations

RECORD_TYPE_MENTION = "social_mention"
RECORD_TYPE_LISTING = "product_listing"

# record_type values that denote a product listing (vs a review/mention).
LISTING_RECORD_TYPES = {RECORD_TYPE_LISTING}


class MentionKind:
    """How a collected mention should be classified."""

    CUSTOMER_REVIEW = "customer_review"
    SUPPLIER_FEEDBACK = "supplier_feedback"


# kind -> (source_type, channel, content_type)
_CLASSIFICATION = {
    MentionKind.CUSTOMER_REVIEW: ("review", "ecommerce_review", "review"),
    MentionKind.SUPPLIER_FEEDBACK: ("supplier_feedback", "supplier_feedback", "feedback"),
}


def classify_mention(kind: str) -> tuple[str, str, str]:
    """Return (source_type, channel, content_type) for a mention kind."""
    return _CLASSIFICATION[kind]
