"""Local Ollama provider (http://localhost:11434)."""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.reporting.llm._http import extract_json, post_json
from app.reporting.llm.base import BaseLLMProvider, LLMError, LLMUnavailable


class OllamaProvider(BaseLLMProvider):
    name = "ollama"

    def __init__(self) -> None:
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model

    async def available(self) -> bool:
        try:
            await post_json(
                f"{self.base_url}/api/tags", payload={}, timeout=5, retries=1
            )
            return True
        except Exception:
            return False

    async def generate_structured(
        self, prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": response_schema or "json",
            "options": {"temperature": settings.llm_temperature},
        }
        data = await post_json(
            f"{self.base_url}/api/generate",
            payload=body,
            timeout=settings.llm_timeout,
            retries=settings.llm_max_retries,
        )
        text = data.get("response")
        if not text:
            raise LLMError("ollama returned no 'response' field")
        return extract_json(text)
