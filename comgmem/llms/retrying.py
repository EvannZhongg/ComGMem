from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from comgmem.config import ModelConfig
from comgmem.llms.base import LLMClient

T = TypeVar("T")


def generate_json_with_parse_retries(
    llm: LLMClient,
    prompt: str,
    parse: Callable[[Any], T],
    *,
    config: ModelConfig | None,
) -> T:
    attempts = max(1, config.retry_attempts if config is not None else 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        payload = llm.generate_json(prompt)
        try:
            return parse(payload)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
    raise last_error  # type: ignore[misc]
