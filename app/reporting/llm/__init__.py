"""Provider-independent LLM integration.

The reporting services depend only on :class:`BaseLLMProvider`. Concrete
providers (Ollama, Gemini, OpenRouter) are selected by configuration; when none
is configured or a call fails, insight generation falls back to a deterministic
narrator so a report is always produced with clearly-labelled provenance and no
fabricated numbers.
"""
