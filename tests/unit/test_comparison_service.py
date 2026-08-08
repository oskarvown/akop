"""Pure unit tests for Stage 4.1 comparison NULL / collision / overdue rules."""
from __future__ import annotations

from decimal import Decimal

from app.application.comparison_service import (
    PositionSnapshot,
    abs_delta,
    compare_position_sets,
    document_bucket_transition,
    overdue_profile_changed,
    percent_delta,
)


def _pos(
    *,
    key: str,
    level: int = 1,
    total_debt: Decimal | None = Decimal("100"),
    overdue: dict[str, Decimal | None] | None = None,
    label: str = "X",
    pid: int = 1,
) -> PositionSnapshot:
    metrics = {
        "document_amount": None,
        "total_debt": total_debt,
        "advance": None,
        "not_due": None,
        "overdue_1_7": None,
        "overdue_8_14": None,
        "overdue_15_21": None,
        "overdue_22_30": None,
        "overdue_over_31": None,
    }
    if overdue:
        metrics.update(overdue)
    return PositionSnapshot(
        id=pid,
        match_key=key,
        match_key_hash="h" * 64,
        outline_level=level,
        raw_label=label,
        counterparty_id=1,
        manager_group_id=1,
        source_file_id=1,
        department="regional",
        metrics=metrics,
        credit_limit=None,
    )


def test_abs_delta_missing_vs_null_metric() -> None:
    # new entity with value
    assert abs_delta(
        curr_present=True,
        prev_present=False,
        curr_m=Decimal("10"),
        prev_m=None,
    ) == Decimal("10")
    # new entity with NULL metric
    assert (
        abs_delta(
            curr_present=True,
            prev_present=False,
            curr_m=None,
            prev_m=None,
        )
        is None
    )
    # both present, one NULL → NULL (not zero)
    assert (
        abs_delta(
            curr_present=True,
            prev_present=True,
            curr_m=Decimal("5"),
            prev_m=None,
        )
        is None
    )
    # closed with prev value
    assert abs_delta(
        curr_present=False,
        prev_present=True,
        curr_m=None,
        prev_m=Decimal("7"),
    ) == Decimal("-7")


def test_percent_only_when_prev_positive() -> None:
    assert percent_delta(Decimal("10"), Decimal("50")) == Decimal("20")
    assert percent_delta(Decimal("10"), Decimal("0")) is None
    assert percent_delta(Decimal("10"), Decimal("-1")) is None
    assert percent_delta(None, Decimal("50")) is None


def test_collision_no_silent_delta() -> None:
    curr = [
        _pos(key="c:1", pid=1, label="A"),
        _pos(key="c:1", pid=2, label="B"),
    ]
    prev = [_pos(key="c:1", pid=3, label="A", total_debt=Decimal("50"))]
    entities, collisions, ambiguous = compare_position_sets(
        current=curr, previous=prev
    )
    assert "c:1" in ambiguous
    assert len(collisions) == 1
    assert collisions[0].cycle_side == "current"
    assert entities[0].ambiguous is True
    assert entities[0].deltas["total_debt"].abs_delta is None


def test_unique_match_builds_delta() -> None:
    curr = [_pos(key="c:1", total_debt=Decimal("120"))]
    prev = [_pos(key="c:1", total_debt=Decimal("100"), pid=2)]
    entities, collisions, ambiguous = compare_position_sets(
        current=curr, previous=prev
    )
    assert not ambiguous and not collisions
    delta = entities[0].deltas["total_debt"]
    assert delta.abs_delta == Decimal("20")
    assert delta.percent_delta == Decimal("20")


def test_overdue_profile_and_l4_bucket_transition() -> None:
    prev = _pos(
        key="c:1|2:a|3:b|4:doc",
        level=4,
        overdue={"overdue_1_7": Decimal("10"), "overdue_8_14": Decimal("0")},
    )
    curr = _pos(
        key="c:1|2:a|3:b|4:doc",
        level=4,
        pid=2,
        overdue={"overdue_1_7": Decimal("0"), "overdue_8_14": Decimal("10")},
    )
    assert overdue_profile_changed(curr, prev) is True
    assert document_bucket_transition(
        outline_level=4, ambiguous=False, curr=curr, prev=prev
    ) == ("overdue_1_7", "overdue_8_14")
    # L1 must not claim document transition
    assert (
        document_bucket_transition(
            outline_level=1, ambiguous=False, curr=curr, prev=prev
        )
        is None
    )
