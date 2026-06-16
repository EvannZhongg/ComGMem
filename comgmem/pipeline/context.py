from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AssemblyContext:
    namespace: str
    metadata: dict[str, Any]
    current_turn: int
