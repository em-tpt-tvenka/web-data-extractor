"""OpenRouter provider (OpenAI-compatible chat completions)."""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.reporting.llm._http import extract_json, post_json
from app.reporting.llm.base import BaseLLMProvider, LLMError, LLMUnavailable


class OpenRouterProvider(BaseLLMProvider):
    name = "openrouter"

    def __init__(self) -> None:
        if not settings.openrouter_api_key:
            raise LLMUnavailable("openrouter_api_key is not set")
        self.api_key = settings.openrouter_api_key
        self.model = settings.openrouter_model
        self.base_url = settings.openrouter_base_url.rstrip("/")

    async def generate_structured(
        self, prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    "You are a market-research analyst. Respond with a single JSON "
                    "object only. Use only the numbers provided; never invent data."},
                {"role": "user", "content": prompt},
            ],
            "temperature": settings.llm_temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://web-data-extractor.local",
            "X-Title": "web-data-extractor reporting",
        }
        data = await post_json(
            f"{self.base_url}/chat/completions",
            payload=body,
            headers=headers,
            timeout=settings.llm_timeout,
            retries=settings.llm_max_retries,
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected OpenRouter response shape: {exc}") from exc
        return extract_json(text)
