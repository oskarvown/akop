"""Pure unit tests for Stage 4.1 comparison NULL / collision / overdue rules."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.application.comparison_service import (
    ADDITIVE_METRICS,
    CycleComparison,
    PositionSnapshot,
    abs_delta,
    build_l1_control_equalities,
    build_source_file_grand_total_equalities,
    compare_position_sets,
    document_bucket_transition,
    overdue_profile_changed,
    percent_delta,
    summarize_comparison,
)


def _pos(
    *,
    key: str,
    level: int = 1,
    total_debt: Decimal | None = Decimal("100"),
    overdue: dict[str, Decimal | None] | None = None,
    label: str = "X",
    pid: int = 1,
    department: str = "regional",
    manager_group_id: int = 1,
    counterparty_id: int = 1,
    source_file_id: int = 1,
    metrics_extra: dict[str, Decimal | None] | None = None,
    credit_limit: Decimal | None = None,
) -> PositionSnapshot:
    metrics = {
        "document_amount": Decimal("0"),
        "total_debt": total_debt,
        "advance": Decimal("0"),
        "not_due": Decimal("0"),
        "overdue_1_7": Decimal("0"),
        "overdue_8_14": Decimal("0"),
        "overdue_15_21": Decimal("0"),
        "overdue_22_30": Decimal("0"),
        "overdue_over_31": Decimal("0"),
    }
    if overdue:
        metrics.update(overdue)
    if metrics_extra:
        metrics.update(metrics_extra)
    return PositionSnapshot(
        id=pid,
        match_key=key,
        match_key_hash="h" * 64,
        outline_level=level,
        raw_label=label,
        counterparty_id=counterparty_id,
        manager_group_id=manager_group_id,
        source_file_id=source_file_id,
        department=department,
        metrics=metrics,
        credit_limit=credit_limit,
    )


def test_abs_delta_missing_vs_null_metric() -> None:
    assert abs_delta(
        curr_present=True,
        prev_present=False,
        curr_m=Decimal("10"),
        prev_m=None,
    ) == Decimal("10")
    assert (
        abs_delta(
            curr_present=True,
            prev_present=False,
            curr_m=None,
            prev_m=None,
        )
        is None
    )
    assert (
        abs_delta(
            curr_present=True,
            prev_present=True,
            curr_m=Decimal("5"),
            prev_m=None,
        )
        is None
    )
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
    assert (
        document_bucket_transition(
            outline_level=1, ambiguous=False, curr=curr, prev=prev
        )
        is None
    )


def test_control_rollups_cover_all_additive_metrics() -> None:
    positions = [
        _pos(
            key="c:1",
            pid=1,
            department="regional",
            manager_group_id=10,
            counterparty_id=100,
            total_debt=Decimal("40"),
            metrics_extra={
                "document_amount": Decimal("40"),
                "advance": Decimal("1"),
                "not_due": Decimal("2"),
                "overdue_1_7": Decimal("3"),
                "overdue_8_14": Decimal("4"),
                "overdue_15_21": Decimal("5"),
                "overdue_22_30": Decimal("6"),
                "overdue_over_31": Decimal("7"),
            },
        ),
        _pos(
            key="c:2",
            pid=2,
            department="moscow",
            manager_group_id=20,
            counterparty_id=200,
            total_debt=Decimal("60"),
            metrics_extra={
                "document_amount": Decimal("60"),
                "advance": Decimal("1"),
                "not_due": Decimal("2"),
                "overdue_1_7": Decimal("3"),
                "overdue_8_14": Decimal("4"),
                "overdue_15_21": Decimal("5"),
                "overdue_22_30": Decimal("6"),
                "overdue_over_31": Decimal("7"),
            },
        ),
        # L2 disclosure — must not enter rollups
        _pos(key="c:1|2:x", pid=3, level=2, total_debt=Decimal("999")),
    ]
    checks = {c.name: c for c in build_l1_control_equalities(positions)}
    for metric in ADDITIVE_METRICS:
        company = checks[f"company_{metric}_vs_sum_departments"]
        assert company.ok is True
        for dept in ("regional", "moscow"):
            dept_check = checks[
                f"department_{dept}_{metric}_vs_sum_manager_groups"
            ]
            assert dept_check.ok is True
        for mg_id in (10, 20):
            mg_check = checks[
                f"manager_group_{mg_id}_{metric}_vs_sum_counterparties"
            ]
            assert mg_check.ok is True
    assert checks["l2_l4_disclosure_only"].diagnostic is True
    # L2 debt must not inflate company totals
    assert checks["company_total_debt_vs_sum_departments"].left == Decimal("100")


def test_source_file_grand_totals_ok_and_mismatch() -> None:
    positions = [
        _pos(
            key="c:1",
            pid=1,
            source_file_id=7,
            total_debt=Decimal("100.00"),
            metrics_extra={"document_amount": Decimal("100.00")},
            credit_limit=Decimal("50.00"),
        )
    ]
    reported_ok = {
        7: {
            "document_amount": "100.00",
            "total_debt": "100.00",
            "advance": "0",
            "not_due": "0",
            "overdue_1_7": "0",
            "overdue_8_14": "0",
            "overdue_15_21": "0",
            "overdue_22_30": "0",
            "overdue_over_31": "0",
            "credit_limit": "50.00",
        }
    }
    ok_checks = build_source_file_grand_total_equalities(positions, reported_ok)
    blocking = [c for c in ok_checks if not c.diagnostic]
    assert all(c.ok for c in blocking)
    credit = next(c for c in ok_checks if c.diagnostic and "credit_limit" in c.name)
    assert credit.ok is True
    assert credit.diagnostic is True

    reported_bad = {7: dict(reported_ok[7])}
    reported_bad[7]["total_debt"] = "999.00"
    bad_checks = build_source_file_grand_total_equalities(positions, reported_bad)
    debt_check = next(
        c
        for c in bad_checks
        if c.name.endswith("total_debt_l1_vs_reported_grand_totals")
    )
    assert debt_check.ok is False
    assert debt_check.diagnostic is False

    summary = summarize_comparison(
        CycleComparison(
            current_cycle_id=1,
            current_report_date=dt.date(2026, 8, 1),
            previous_cycle_id=None,
            previous_report_date=None,
            entities=(),
            collisions=(),
            control_equalities=tuple(bad_checks),
            ambiguous_keys=frozenset(),
        )
    )
    assert debt_check.name in summary.control_failures

    reported_credit_mismatch = {7: dict(reported_ok[7])}
    reported_credit_mismatch[7]["credit_limit"] = "1.00"
    credit_checks = build_source_file_grand_total_equalities(
        positions, reported_credit_mismatch
    )
    credit_bad = next(c for c in credit_checks if c.diagnostic)
    assert credit_bad.ok is False
    summary2 = summarize_comparison(
        CycleComparison(
            current_cycle_id=1,
            current_report_date=dt.date(2026, 8, 1),
            previous_cycle_id=None,
            previous_report_date=None,
            entities=(),
            collisions=(),
            control_equalities=tuple(credit_checks),
            ambiguous_keys=frozenset(),
        )
    )
    assert credit_bad.name not in summary2.control_failures
