from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from comgmem.config import LoggingConfig


class NamespaceMemoryLogWriter:
    def __init__(self, config: LoggingConfig, *, base_path: str | Path) -> None:
        self.config = config
        self.base_path = Path(base_path)

    def write(self, namespace: str, record: dict[str, Any]) -> None:
        if not self.config.enabled:
            return
        namespace_path = self.base_path / quote(namespace, safe="")
        namespace_path.mkdir(parents=True, exist_ok=True)
        log_path = namespace_path / "memory_writes.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
