"""Unit tests for Stage 4.1 match_key builders."""
from __future__ import annotations

import pytest

from app.domain.matching import build_match_key, match_key_hash, normalize_name


def test_build_match_key_levels() -> None:
    l1 = build_match_key(
        counterparty_id=42,
        outline_level=1,
        normalized_label="ignored",
        parent_match_key=None,
    )
    assert l1 == "c:42"

    l2 = build_match_key(
        counterparty_id=42,
        outline_level=2,
        normalized_label=normalize_name("Договор №1"),
        parent_match_key=l1,
    )
    assert l2.startswith("c:42|2:")

    l3 = build_match_key(
        counterparty_id=42,
        outline_level=3,
        normalized_label=normalize_name("Объект"),
        parent_match_key=l2,
    )
    assert "|3:" in l3

    l4 = build_match_key(
        counterparty_id=42,
        outline_level=4,
        normalized_label=normalize_name("УПД-1"),
        parent_match_key=l3,
    )
    assert "|4:" in l4
    assert match_key_hash(l4) == match_key_hash(l4)
    assert len(match_key_hash(l4)) == 64


def test_build_match_key_requires_parent_above_l1() -> None:
    with pytest.raises(ValueError, match="parent_match_key"):
        build_match_key(
            counterparty_id=1,
            outline_level=2,
            normalized_label="x",
            parent_match_key=None,
        )
