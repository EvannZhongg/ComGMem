from __future__ import annotations

import re
import unicodedata


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def compact_key(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", text)
    return text.strip("_")


def tokenize(value: str | None) -> list[str]:
    text = normalize_text(value)
    return [match.group(0) for match in _TOKEN_RE.finditer(text)]


def truncate(text: str, max_chars: int = 240) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
