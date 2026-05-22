from __future__ import annotations

from typing import Any

from c_hypermem.config import OpenAICompatibleModelConfig
from c_hypermem.errors import ConfigError


class EmbeddingModelClient:
    """Generic embedding model client entry point.

    The current implementation speaks the OpenAI-compatible embeddings API, while
    keeping the module and class name provider-neutral for future model backends.
    """

    def __init__(self, config: OpenAICompatibleModelConfig) -> None:
        self.config = config
        self._client: Any | None = None

    @classmethod
    def from_config(cls, config: OpenAICompatibleModelConfig | dict[str, Any]) -> "EmbeddingModelClient":
        return cls(OpenAICompatibleModelConfig.model_validate(config))

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigError("Install c-hypermem[embeddings] to use embedding model calls.") from exc
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.config.model, input=texts)
        return [item.embedding for item in response.data]

