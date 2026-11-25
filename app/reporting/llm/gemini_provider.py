"""Google Gemini provider (free tier via generativelanguage API)."""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.reporting.llm._http import extract_json, post_json
from app.reporting.llm.base import BaseLLMProvider, LLMError, LLMUnavailable


def _sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's responseSchema is a restricted OpenAPI subset: drop keys it
    rejects (``$defs``/``$ref``/``additionalProperties``/``title``)."""
    drop = {"$defs", "$ref", "additionalProperties", "title", "default"}
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in drop:
            continue
        if isinstance(v, dict):
            out[k] = _sanitize_schema(v)
        elif isinstance(v, list):
            out[k] = [_sanitize_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


class GeminiProvider(BaseLLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise LLMUnavailable("gemini_api_key is not set")
        self.api_key = settings.gemini_api_key
        self.model = settings.gemini_model
        self.base_url = settings.gemini_base_url.rstrip("/")

    async def generate_structured(
        self, prompt: str, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        gen_config: dict[str, Any] = {
            "temperature": settings.llm_temperature,
            "responseMimeType": "application/json",
        }
        if response_schema:
            gen_config["responseSchema"] = _sanitize_schema(response_schema)
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        data = await post_json(
            url,
            payload=body,
            params={"key": self.api_key},
            timeout=settings.llm_timeout,
            retries=settings.llm_max_retries,
        )
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected Gemini response shape: {exc}") from exc
        return extract_json(text)
