"""Local, deterministic NLP for review classification.

No network calls and no heavy ML dependencies: sentiment is derived from the
star rating and refined with a polarity lexicon; aspects and topics come from a
configurable keyword taxonomy. This keeps per-review classification cheap and
reproducible so the same reviews are never re-sent to an LLM for scoring.
"""
