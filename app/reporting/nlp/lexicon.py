"""Polarity and complaint lexicons used to refine rating-based sentiment and to
classify complaints. Deliberately small and auditable; extend as needed."""

from __future__ import annotations

POSITIVE_WORDS: frozenset[str] = frozenset({
    "good", "great", "excellent", "amazing", "awesome", "love", "loved", "best",
    "perfect", "happy", "satisfied", "superb", "fantastic", "nice", "wonderful",
    "recommend", "recommended", "worth", "durable", "fast", "smooth", "reliable",
    "value", "comfortable", "beautiful", "premium", "impressed", "flawless",
    "quick", "easy", "sturdy", "genuine", "original", "accha", "badhiya", "mast",
})

NEGATIVE_WORDS: frozenset[str] = frozenset({
    "bad", "poor", "terrible", "worst", "horrible", "awful", "hate", "disappointed",
    "disappointing", "defective", "faulty", "broken", "broke", "damaged", "cheap",
    "useless", "waste", "slow", "lag", "problem", "issue", "fail", "failed", "stopped",
    "refund", "return", "fake", "duplicate", "scam", "delay", "delayed", "late",
    "overpriced", "noisy", "heating", "drains", "rejected", "unreachable", "pathetic",
    "regret", "worse", "ghatiya", "bekar", "kharab",
})

NEGATORS: frozenset[str] = frozenset({"not", "no", "never", "n't", "without", "hardly", "nahi"})

# Complaint category -> trigger keywords. A complaint is counted only on a
# negative/mixed review, so these are intentionally broad.
COMPLAINT_CATEGORIES: dict[str, list[str]] = {
    "delivery": ["delivery", "shipping", "late", "delayed", "courier", "not delivered",
                 "took too long", "arrived late"],
    "product_defect": ["defective", "faulty", "broken", "broke", "damaged", "not working",
                       "stopped working", "dead", "malfunction"],
    "battery": ["battery", "drains", "charge", "charging", "backup", "battery life"],
    "performance": ["slow", "lag", "hang", "heating", "heats", "noise", "noisy",
                    "overheat", "cooling"],
    "installation": ["installation", "install", "technician", "no-show", "no show",
                     "not installed", "installation delay"],
    "warranty": ["warranty", "warranty claim", "warranty reject", "guarantee",
                 "warranty rejected"],
    "refund_replacement": ["refund", "replacement", "replace", "return", "money back",
                           "not refunded", "exchange"],
    "customer_service": ["customer service", "customer care", "support", "call center",
                         "call centre", "unreachable", "no response", "rude"],
}
