from __future__ import annotations

from comgmem.errors import ConfigError


class TokenCounter:
    def count(self, text: str) -> int:
        raise NotImplementedError


class TikTokenCounter(TokenCounter):
    def __init__(self, encoding_name: str) -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise ConfigError("Install tiktoken to use token-limit compaction.") from exc
        self.encoding = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text))
