"""Deterministic review sentiment.

Primary signal is the 1-5 star ``rating``; the text lexicon only (a) classifies
reviews that have no rating and (b) upgrades a strong-rating review to ``mixed``
when the text carries clear opposite-polarity signal. The denominator and the
treatment of mixed/unclassified reviews are defined here and surfaced verbatim
in the report methodology:

    classified = positive + negative + neutral + mixed
    net_sentiment (%) = positive/classified*100 - negative/classified*100
    'unclassified' (no rating and no lexical signal) is excluded from every
    percentage but counted in data coverage.
"""

from __future__ import annotations

import re

from app.reporting.constants import Sentiment
from app.reporting.nlp.lexicon import NEGATIVE_WORDS, NEGATORS, POSITIVE_WORDS

_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")

# A review with >= this many opposite-polarity hits (against a strong rating) is
# re-labelled 'mixed' rather than taking the rating at face value.
_MIXED_MIN_OPPOSITE = 2


def _polarity_score(text: str) -> tuple[int, int]:
    """Return ``(positive_hits, negative_hits)`` with simple negation handling."""
    tokens = _TOKEN_RE.findall((text or "").lower())
    pos = neg = 0
    for i, tok in enumerate(tokens):
        negated = i > 0 and tokens[i - 1] in NEGATORS
        if tok in POSITIVE_WORDS:
            neg += 1 if negated else 0
            pos += 0 if negated else 1
        elif tok in NEGATIVE_WORDS:
            pos += 1 if negated else 0
            neg += 0 if negated else 1
    return pos, neg


def classify(rating: float | int | None, text: str | None) -> str:
    """Classify one review. Returns a :class:`Sentiment` value (as ``str``)."""
    pos, neg = _polarity_score(text or "")

    if rating is not None:
        try:
            r = float(rating)
        except (TypeError, ValueError):
            r = None
        if r is not None:
            if r >= 4:
                base = Sentiment.positive
                opposite = neg
            elif r <= 2:
                base = Sentiment.negative
                opposite = pos
            else:
                # 3-star: let the text break the tie, else neutral.
                if pos and pos > neg:
                    return Sentiment.positive.value
                if neg and neg > pos:
                    return Sentiment.negative.value
                return Sentiment.neutral.value
            if opposite >= _MIXED_MIN_OPPOSITE:
                return Sentiment.mixed.value
            return base.value

    # No usable rating: fall back to the lexicon alone.
    if pos == 0 and neg == 0:
        return Sentiment.unclassified.value
    if pos and neg and abs(pos - neg) <= 1:
        return Sentiment.mixed.value
    return Sentiment.positive.value if pos > neg else Sentiment.negative.value
