"""Abstract LLM provider contract and shared error types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """Any provider-side failure (network, timeout, rate limit, bad JSON)."""


class LLMUnavailable(LLMError):
    """Provider is not configured or cannot be reached."""


class BaseLLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def generate_structured(
        self, prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a JSON object conforming to ``response_schema``.

        Must raise :class:`LLMError` (never return fabricated content) on any
        failure so the caller can fall back deterministically.
        """

    async def available(self) -> bool:
        """Cheap readiness probe. Default: assume available."""
        return True
