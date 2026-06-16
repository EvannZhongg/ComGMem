from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_hash(*parts: Any, length: int = 16) -> str:
    payload = json.dumps(_canonical(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_canonical(item) for item in value]
    return value

