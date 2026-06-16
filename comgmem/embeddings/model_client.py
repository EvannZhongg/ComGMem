from __future__ import annotations

import time
from typing import Any

from comgmem.config import ModelConfig
from comgmem.errors import ConfigError


class EmbeddingModelClient:
    """Generic embedding model client entry point.

    The current implementation speaks the OpenAI-compatible embeddings API, while
    keeping the module and class name provider-neutral for future model backends.
    """

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._client: Any | None = None

    @classmethod
    def from_config(cls, config: ModelConfig | dict[str, Any]) -> "EmbeddingModelClient":
        return cls(ModelConfig.model_validate(config))

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigError("Install comgmem[embeddings] to use embedding model calls.") from exc
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        batch_size = max(1, self.config.batch_size)
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            response = _with_retries(
                lambda: self.client.embeddings.create(model=self.config.model, input=batch),
                config=self.config,
            )
            embeddings.extend(item.embedding for item in response.data)
        return embeddings


def _with_retries(call, *, config: ModelConfig) -> Any:
    attempts = max(1, config.retry_attempts)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not _is_retryable_openai_error(exc):
                raise
            delay = min(config.retry_backoff_max_sec, config.retry_backoff_base_sec ** attempt)
            time.sleep(max(0.0, delay))
    raise last_error  # type: ignore[misc]


def _is_retryable_openai_error(exc: Exception) -> bool:
    try:
        from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    except ImportError:
        return False
    return isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError))
