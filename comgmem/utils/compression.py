from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from comgmem.config import MemoryConfig
from comgmem.llms.base import LLMClient
from comgmem.llms.retrying import generate_json_with_parse_retries
from comgmem.utils.prompts import PromptRegistry
from comgmem.utils.token_counting import TikTokenCounter, TokenCounter


@dataclass(frozen=True)
class CompressionResult:
    text: str
    original_token_count: int
    compressed_token_count: int
    prompt_token_count: int
    max_tokens: int


class TokenLimitCompressor:
    def __init__(
        self,
        config: MemoryConfig,
        *,
        llm: LLMClient | None = None,
        prompt_registry: PromptRegistry | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.prompt_registry = prompt_registry or PromptRegistry()
        self._token_counter = token_counter

    def compress_if_needed(
        self,
        text: str,
        *,
        max_tokens: int | None,
        content_type: str,
    ) -> CompressionResult | None:
        if max_tokens is None or not text.strip():
            return None
        token_count = self._count_tokens(text)
        if token_count <= max_tokens:
            return None
        if self.llm is None:
            raise RuntimeError("Token-limit compaction was triggered and requires an LLM.")
        prompt = self._render_prompt(
            text,
            content_type=content_type,
            token_count=token_count,
            max_tokens=max_tokens,
        )
        prompt_token_count = self._count_tokens(prompt)
        result = generate_json_with_parse_retries(
            self.llm,
            prompt,
            _parse_token_limit_compaction_result,
            config=self.config.llm,
        )
        compressed_text = result.compressed_text.strip()
        return CompressionResult(
            text=compressed_text,
            original_token_count=token_count,
            compressed_token_count=self._count_tokens(compressed_text),
            prompt_token_count=prompt_token_count,
            max_tokens=max_tokens,
        )

    def _render_prompt(self, text: str, *, content_type: str, token_count: int, max_tokens: int) -> str:
        prompt = self.prompt_registry.load("compression.token_limit_compaction")
        replacements = {
            "{{CONTENT_TYPE}}": content_type,
            "{{ORIGINAL_TOKEN_COUNT}}": str(token_count),
            "{{TARGET_MAX_TOKENS}}": str(max_tokens),
            "{{SOURCE_TEXT}}": text,
            "{{STRICT_JSON_SHAPE}}": 'Return exactly one JSON object: {"compressed_text": "..."}',
        }
        rendered = prompt.text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered

    def _count_tokens(self, text: str) -> int:
        if self._token_counter is None:
            self._token_counter = TikTokenCounter(self.config.token_counting.tokenizer_encoding)
        return self._token_counter.count(text)


class TokenLimitCompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compressed_text: str


def _parse_token_limit_compaction_result(payload: dict[str, Any]) -> TokenLimitCompactionResult:
    if not isinstance(payload, dict):
        raise ValueError("Token-limit compaction payload must be a JSON object.")
    return TokenLimitCompactionResult.model_validate(payload)
