"""Language detection for extracted content (shared normalization layer).

Detects the actual language of scraped text rather than assuming the requested
search language. Returns an ISO 639-1 code, or None when the text is empty or
detection is not reliable. Content is never translated.
"""

from __future__ import annotations

from langdetect import DetectorFactory, LangDetectException, detect

# Make detection deterministic across runs.
DetectorFactory.seed = 0

_MIN_CHARS = 3

# langdetect emits a few region-tagged codes; fold them to ISO 639-1.
_NORMALIZE = {"zh-cn": "zh", "zh-tw": "zh"}


def detect_language(text: str | None) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS:
        return None
    try:
        code = detect(stripped)
    except LangDetectException:
        return None
    return _NORMALIZE.get(code, code)
