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
from typing import Any

from app.domain.matching.normalization import normalize_name


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


def backfill_match_keys_on_connection(conn: Any) -> int:
    """Fill normalized_label/match_key/match_key_hash for all debt_positions.

    Used by Alembic migration and integration backfill tests. Processes rows in
    outline_level order so parent keys exist before children.
    Returns number of updated rows.
    """
    from sqlalchemy import text

    rows = conn.execute(
        text(
            "SELECT id, counterparty_id, parent_position_id, outline_level, raw_label "
            "FROM debt_positions ORDER BY outline_level ASC, id ASC"
        )
    ).fetchall()

    key_by_id: dict[int, str] = {}
    for row in rows:
        position_id = int(row[0])
        counterparty_id = int(row[1])
        parent_id = row[2]
        outline_level = int(row[3])
        raw_label = str(row[4])
        normalized = normalize_name(raw_label)
        key = build_match_key(
            counterparty_id=counterparty_id,
            outline_level=outline_level,
            normalized_label=normalized,
            parent_match_key=(
                key_by_id[int(parent_id)] if parent_id is not None else None
            ),
        )
        key_by_id[position_id] = key
        conn.execute(
            text(
                "UPDATE debt_positions "
                "SET normalized_label = :normalized_label, "
                "match_key = :match_key, "
                "match_key_hash = :match_key_hash "
                "WHERE id = :id"
            ),
            {
                "id": position_id,
                "normalized_label": normalized,
                "match_key": key,
                "match_key_hash": match_key_hash(key),
            },
        )

    nulls = conn.execute(
        text(
            "SELECT COUNT(*) FROM debt_positions "
            "WHERE normalized_label IS NULL OR match_key IS NULL "
            "OR match_key_hash IS NULL"
        )
    ).scalar()
    if nulls:
        raise RuntimeError(f"match_key backfill left {nulls} NULL rows")
    return len(rows)
