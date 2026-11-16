from __future__ import annotations

RECORD_TYPE_MENTION = "social_mention"
RECORD_TYPE_LISTING = "product_listing"

LISTING_RECORD_TYPES = {RECORD_TYPE_LISTING}

class MentionKind:
    CUSTOMER_REVIEW = "customer_review"
    SUPPLIER_FEEDBACK = "supplier_feedback"

_CLASSIFICATION = {
    MentionKind.CUSTOMER_REVIEW: ("review", "ecommerce_review", "review"),
    MentionKind.SUPPLIER_FEEDBACK: ("supplier_feedback", "supplier_feedback", "feedback"),
}

def classify_mention(kind: str) -> tuple[str, str, str]:
    return _CLASSIFICATION[kind]
