from __future__ import annotations

import json
import time
from typing import Any

from comgmem.config import ModelConfig
from comgmem.errors import ConfigError


class OpenAICompatibleLLM:
    """Small OpenAI-compatible chat client for explicit extractor implementations."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._client: Any | None = None

    @classmethod
    def from_config(cls, config: ModelConfig | dict[str, Any]) -> "OpenAICompatibleLLM":
        return cls(ModelConfig.model_validate(config))

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigError("Install comgmem[llms] to use OpenAI-compatible LLM calls.") from exc
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    def generate_json(self, prompt: str) -> dict[str, Any]:
        return _with_retries(
            lambda: _parse_json_response(
                self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
            ),
            config=self.config,
        )


def _parse_json_response(response: Any) -> dict[str, Any]:
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def _with_retries(call, *, config: ModelConfig) -> Any:
    attempts = max(1, config.retry_attempts)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not _is_retryable_error(exc):
                raise
            delay = min(config.retry_backoff_max_sec, config.retry_backoff_base_sec ** attempt)
            time.sleep(max(0.0, delay))
    raise last_error  # type: ignore[misc]


def _is_retryable_error(exc: Exception) -> bool:
    return isinstance(exc, json.JSONDecodeError) or _is_retryable_openai_error(exc)


def _is_retryable_openai_error(exc: Exception) -> bool:
    try:
        from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    except ImportError:
        return False
    return isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError))
