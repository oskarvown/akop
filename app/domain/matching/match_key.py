"""Match keys for cross-cycle debt position comparison (Stage 4.1).

Path format (row_order is never part of the key):

```text
L1: c:{counterparty_id}
L2: c:{counterparty_id}|2:{normalized_label}
L3: …|3:{normalized_label}
L4: …|4:{normalized_label}
```
"""
from __future__ import annotations

import hashlib


def build_match_key(
    *,
    counterparty_id: int,
    outline_level: int,
    normalized_label: str,
    parent_match_key: str | None,
) -> str:
    if outline_level < 1 or outline_level > 4:
        raise ValueError(f"outline_level must be 1..4, got {outline_level}")
    if outline_level == 1:
        return f"c:{counterparty_id}"
    if parent_match_key is None:
        raise ValueError("parent_match_key is required for outline_level > 1")
    return f"{parent_match_key}|{outline_level}:{normalized_label}"


def match_key_hash(match_key: str) -> str:
    return hashlib.sha256(match_key.encode("utf-8")).hexdigest()
