from __future__ import annotations

from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0

_MIN_CHARS = 3

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
