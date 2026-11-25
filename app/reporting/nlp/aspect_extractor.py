"""Aspect / topic extraction and complaint tagging over review text.

Keyword-driven against the configurable taxonomy. Aspect sentiment inherits the
review's overall sentiment (a documented simplification: per-aspect polarity
windows are noisy on short marketplace reviews and would overstate precision).
"""

from __future__ import annotations

from app.reporting.constants import Sentiment
from app.reporting.nlp.lexicon import COMPLAINT_CATEGORIES
from app.reporting.nlp.topic_taxonomy import build_keyword_index

_KEYWORD_INDEX = build_keyword_index()


def extract_aspects(text: str | None, sentiment: str) -> list[dict]:
    """Return ``[{aspect, category, theme, sentiment}, ...]`` for one review.

    ``aspect`` is the matched theme; ``category`` is its level-1 parent. Each
    theme is reported at most once per review.
    """
    if not text:
        return []
    low = text.lower()
    seen: set[str] = set()
    out: list[dict] = []
    for keyword, level_1, theme in _KEYWORD_INDEX:
        if theme in seen:
            continue
        if keyword in low:
            seen.add(theme)
            out.append({
                "aspect": theme,
                "category": level_1,
                "theme": theme,
                "sentiment": sentiment,
            })
    return out


def extract_complaints(text: str | None, sentiment: str) -> list[str]:
    """Return complaint categories present in a *negative or mixed* review.

    Positive/neutral reviews yield no complaints even if a keyword appears, so a
    complaint count never includes praise that merely mentions the aspect.
    """
    if not text or sentiment not in (Sentiment.negative.value, Sentiment.mixed.value):
        return []
    low = text.lower()
    found: list[str] = []
    for category, keywords in COMPLAINT_CATEGORIES.items():
        if any(kw in low for kw in keywords):
            found.append(category)
    return found
