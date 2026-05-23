from __future__ import annotations

import json
from typing import Any

from c_hypermem.config import ModelConfig
from c_hypermem.errors import ConfigError


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
                raise ConfigError("Install c-hypermem[llms] to use OpenAI-compatible LLM calls.") from exc
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    def generate_json(self, prompt: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
