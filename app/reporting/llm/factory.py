"""Select an LLM provider by name. Returns ``None`` for the deterministic
fallback (explicit ``fallback``, unknown name, or an unconfigured provider)."""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.reporting.llm.base import BaseLLMProvider, LLMUnavailable

_log = get_logger("reporting.llm.factory")


def get_provider(name: str | None = None) -> BaseLLMProvider | None:
    provider = (name or settings.llm_provider or "fallback").lower()
    if provider in ("fallback", "none", "deterministic", ""):
        return None
    try:
        if provider == "ollama":
            from app.reporting.llm.ollama_provider import OllamaProvider
            return OllamaProvider()
        if provider == "gemini":
            from app.reporting.llm.gemini_provider import GeminiProvider
            return GeminiProvider()
        if provider == "openrouter":
            from app.reporting.llm.openrouter_provider import OpenRouterProvider
            return OpenRouterProvider()
    except LLMUnavailable as exc:
        _log.warning("event=provider_unavailable provider=%s reason=%s", provider, exc)
        return None
    _log.warning("event=unknown_provider provider=%s falling_back", provider)
    return None
