"""Deterministic period-over-period debt comparison (Stage 4.1).

No Excel / Telegram / LLM. Pure Decimal arithmetic with strict NULL semantics.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    AuditCycle,
    AuditCycleStatus,
    Counterparty,
    DebtPosition,
    ManagerGroup,
    SourceFile,
    SourceFileLifecycle,
)

ADDITIVE_METRICS: tuple[str, ...] = (
    "document_amount",
    "total_debt",
    "advance",
    "not_due",
    "overdue_1_7",
    "overdue_8_14",
    "overdue_15_21",
    "overdue_22_30",
    "overdue_over_31",
)

OVERDUE_BUCKETS: tuple[str, ...] = (
    "overdue_1_7",
    "overdue_8_14",
    "overdue_15_21",
    "overdue_22_30",
    "overdue_over_31",
)

CONTROL_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class MetricDelta:
    current: Decimal | None
    previous: Decimal | None
    abs_delta: Decimal | None
    percent_delta: Decimal | None  # None → display as «н/д»


@dataclass(frozen=True)
class PositionSnapshot:
    id: int
    match_key: str
    match_key_hash: str
    outline_level: int
    raw_label: str
    counterparty_id: int
    manager_group_id: int
    source_file_id: int
    department: str
    metrics: dict[str, Decimal | None]
    credit_limit: Decimal | None
    parent_position_id: int | None = None
    manager_group_name: str = ""
    counterparty_name: str = ""


@dataclass(frozen=True)
class MatchedEntity:
    match_key: str
    outline_level: int
    current: PositionSnapshot | None
    previous: PositionSnapshot | None
    ambiguous: bool
    collision_current_count: int
    collision_previous_count: int
    deltas: dict[str, MetricDelta]
    overdue_profile_changed: bool
    document_bucket_transition: tuple[str, str] | None
    """(from_bucket, to_bucket) only when unique L4 match and exactly one bucket each side."""


@dataclass(frozen=True)
class ControlCollision:
    match_key: str
    cycle_side: str  # current | previous
    count: int
    raw_labels: tuple[str, ...]


@dataclass(frozen=True)
class ControlEquality:
    name: str
    left: Decimal | None
    right: Decimal | None
    ok: bool
    diagnostic: bool = False


@dataclass(frozen=True)
class CycleComparison:
    current_cycle_id: int
    current_report_date: dt.date
    previous_cycle_id: int | None
    previous_report_date: dt.date | None
    entities: tuple[MatchedEntity, ...]
    collisions: tuple[ControlCollision, ...]
    control_equalities: tuple[ControlEquality, ...]
    ambiguous_keys: frozenset[str]
    # Raw L1–L4 snapshots for accounting aggregates (includes collision rows).
    current_positions: tuple[PositionSnapshot, ...] = ()
    previous_positions: tuple[PositionSnapshot, ...] = ()


def abs_delta(
    *,
    curr_present: bool,
    prev_present: bool,
    curr_m: Decimal | None,
    prev_m: Decimal | None,
) -> Decimal | None:
    """Strict NULL / missing-entity semantics from Stage 4 plan §3."""
    if not curr_present and not prev_present:
        return None
    if curr_present and not prev_present:
        return curr_m  # NULL stays NULL
    if prev_present and not curr_present:
        if prev_m is None:
            return None
        return Decimal("0") - prev_m
    # both present
    if curr_m is None or prev_m is None:
        return None
    return curr_m - prev_m


def percent_delta(abs_d: Decimal | None, prev_m: Decimal | None) -> Decimal | None:
    if abs_d is None or prev_m is None or prev_m <= 0:
        return None
    return (abs_d / prev_m) * Decimal("100")


def compute_metric_deltas(
    *,
    curr: PositionSnapshot | None,
    prev: PositionSnapshot | None,
) -> dict[str, MetricDelta]:
    curr_present = curr is not None
    prev_present = prev is not None
    out: dict[str, MetricDelta] = {}
    for name in ADDITIVE_METRICS:
        curr_m = curr.metrics.get(name) if curr is not None else None
        prev_m = prev.metrics.get(name) if prev is not None else None
        ad = abs_delta(
            curr_present=curr_present,
            prev_present=prev_present,
            curr_m=curr_m,
            prev_m=prev_m,
        )
        out[name] = MetricDelta(
            current=curr_m if curr_present else None,
            previous=prev_m if prev_present else None,
            abs_delta=ad,
            percent_delta=percent_delta(ad, prev_m if prev_present else None),
        )
    return out


def overdue_vector(metrics: dict[str, Decimal | None]) -> tuple[Decimal | None, ...]:
    return tuple(metrics.get(name) for name in OVERDUE_BUCKETS)


def overdue_profile_changed(
    curr: PositionSnapshot | None, prev: PositionSnapshot | None
) -> bool:
    if curr is None or prev is None:
        return False
    return overdue_vector(curr.metrics) != overdue_vector(prev.metrics)


def _nonzero_buckets(metrics: dict[str, Decimal | None]) -> list[str]:
    found: list[str] = []
    for name in OVERDUE_BUCKETS:
        value = metrics.get(name)
        if value is not None and value != 0:
            found.append(name)
    return found


def document_bucket_transition(
    *,
    outline_level: int,
    ambiguous: bool,
    curr: PositionSnapshot | None,
    prev: PositionSnapshot | None,
) -> tuple[str, str] | None:
    """Only unique L4 matches may claim a document moved between overdue buckets."""
    if ambiguous or outline_level != 4 or curr is None or prev is None:
        return None
    curr_b = _nonzero_buckets(curr.metrics)
    prev_b = _nonzero_buckets(prev.metrics)
    if len(curr_b) == 1 and len(prev_b) == 1 and curr_b[0] != prev_b[0]:
        return (prev_b[0], curr_b[0])
    return None


def _sum_metrics(
    positions: Iterable[PositionSnapshot], metric: str
) -> Decimal | None:
    total = Decimal("0")
    any_value = False
    for pos in positions:
        value = pos.metrics.get(metric)
        if value is None:
            continue
        total += value
        any_value = True
    return total if any_value else None


def _snapshot_from_row(
    row: DebtPosition,
    *,
    department: str,
    manager_group_name: str = "",
    counterparty_name: str = "",
) -> PositionSnapshot:
    metrics = {name: getattr(row, name) for name in ADDITIVE_METRICS}
    return PositionSnapshot(
        id=row.id,
        match_key=row.match_key,
        match_key_hash=row.match_key_hash,
        outline_level=row.outline_level,
        raw_label=row.raw_label,
        counterparty_id=row.counterparty_id,
        manager_group_id=row.manager_group_id,
        source_file_id=row.source_file_id,
        department=department,
        metrics=metrics,
        credit_limit=row.credit_limit,
        parent_position_id=row.parent_position_id,
        manager_group_name=manager_group_name,
        counterparty_name=counterparty_name,
    )


async def find_previous_completed_cycle(
    session: AsyncSession,
    *,
    current_report_date: dt.date,
) -> AuditCycle | None:
    return await session.scalar(
        select(AuditCycle)
        .where(
            AuditCycle.status == AuditCycleStatus.COMPLETED,
            AuditCycle.report_date < current_report_date,
        )
        .order_by(AuditCycle.report_date.desc())
        .limit(1)
    )


async def load_active_positions(
    session: AsyncSession, cycle_id: int
) -> list[PositionSnapshot]:
    rows = (
        await session.execute(
            select(
                DebtPosition,
                SourceFile.department,
                ManagerGroup.raw_name,
                Counterparty.raw_name,
            )
            .join(SourceFile, DebtPosition.source_file_id == SourceFile.id)
            .join(ManagerGroup, DebtPosition.manager_group_id == ManagerGroup.id)
            .join(Counterparty, DebtPosition.counterparty_id == Counterparty.id)
            .where(
                SourceFile.audit_cycle_id == cycle_id,
                SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
            )
            .order_by(DebtPosition.id)
        )
    ).all()
    return [
        _snapshot_from_row(
            position,
            department=department.value,
            manager_group_name=mg_name,
            counterparty_name=cp_name,
        )
        for position, department, mg_name, cp_name in rows
    ]


def _index_by_match_key(
    positions: Sequence[PositionSnapshot],
) -> dict[str, list[PositionSnapshot]]:
    indexed: dict[str, list[PositionSnapshot]] = defaultdict(list)
    for pos in positions:
        indexed[pos.match_key].append(pos)
    return indexed


def compare_position_sets(
    *,
    current: Sequence[PositionSnapshot],
    previous: Sequence[PositionSnapshot],
) -> tuple[tuple[MatchedEntity, ...], tuple[ControlCollision, ...], frozenset[str]]:
    curr_idx = _index_by_match_key(current)
    prev_idx = _index_by_match_key(previous)
    all_keys = sorted(set(curr_idx) | set(prev_idx))

    entities: list[MatchedEntity] = []
    collisions: list[ControlCollision] = []
    ambiguous: set[str] = set()

    for key in all_keys:
        curr_list = curr_idx.get(key, [])
        prev_list = prev_idx.get(key, [])
        curr_ambiguous = len(curr_list) > 1
        prev_ambiguous = len(prev_list) > 1
        is_ambiguous = curr_ambiguous or prev_ambiguous

        if curr_ambiguous:
            ambiguous.add(key)
            collisions.append(
                ControlCollision(
                    match_key=key,
                    cycle_side="current",
                    count=len(curr_list),
                    raw_labels=tuple(p.raw_label for p in curr_list),
                )
            )
        if prev_ambiguous:
            ambiguous.add(key)
            collisions.append(
                ControlCollision(
                    match_key=key,
                    cycle_side="previous",
                    count=len(prev_list),
                    raw_labels=tuple(p.raw_label for p in prev_list),
                )
            )

        curr_one = curr_list[0] if len(curr_list) == 1 else None
        prev_one = prev_list[0] if len(prev_list) == 1 else None
        level = (
            curr_one.outline_level
            if curr_one is not None
            else prev_one.outline_level
            if prev_one is not None
            else curr_list[0].outline_level
            if curr_list
            else prev_list[0].outline_level
        )

        if is_ambiguous:
            # No silent entity deltas for ambiguous keys.
            deltas = {
                name: MetricDelta(
                    current=None, previous=None, abs_delta=None, percent_delta=None
                )
                for name in ADDITIVE_METRICS
            }
            entities.append(
                MatchedEntity(
                    match_key=key,
                    outline_level=level,
                    current=None,
                    previous=None,
                    ambiguous=True,
                    collision_current_count=len(curr_list),
                    collision_previous_count=len(prev_list),
                    deltas=deltas,
                    overdue_profile_changed=False,
                    document_bucket_transition=None,
                )
            )
            continue

        deltas = compute_metric_deltas(curr=curr_one, prev=prev_one)
        entities.append(
            MatchedEntity(
                match_key=key,
                outline_level=level,
                current=curr_one,
                previous=prev_one,
                ambiguous=False,
                collision_current_count=len(curr_list),
                collision_previous_count=len(prev_list),
                deltas=deltas,
                overdue_profile_changed=overdue_profile_changed(curr_one, prev_one),
                document_bucket_transition=document_bucket_transition(
                    outline_level=level,
                    ambiguous=False,
                    curr=curr_one,
                    prev=prev_one,
                ),
            )
        )

    return tuple(entities), tuple(collisions), frozenset(ambiguous)


def _near(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(left - right) <= CONTROL_TOLERANCE


def _parse_reported_metric(raw: object | None) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    return Decimal(text)


def _sum_credit_limit(positions: Iterable[PositionSnapshot]) -> Decimal | None:
    total = Decimal("0")
    any_value = False
    for pos in positions:
        if pos.credit_limit is None:
            continue
        total += pos.credit_limit
        any_value = True
    return total if any_value else None


def build_l1_control_equalities(
    current: Sequence[PositionSnapshot],
) -> tuple[ControlEquality, ...]:
    """L1 rollups for every additive metric; L2–L4 are disclosure-only."""
    l1 = [p for p in current if p.outline_level == 1]
    checks: list[ControlEquality] = []
    by_dept: dict[str, list[PositionSnapshot]] = defaultdict(list)
    for pos in l1:
        by_dept[pos.department].append(pos)

    for metric in ADDITIVE_METRICS:
        company_total = _sum_metrics(l1, metric)
        dept_sum = Decimal("0")
        any_dept = False
        for positions in by_dept.values():
            part = _sum_metrics(positions, metric)
            if part is not None:
                dept_sum += part
                any_dept = True
        dept_total = dept_sum if any_dept else None
        checks.append(
            ControlEquality(
                name=f"company_{metric}_vs_sum_departments",
                left=company_total,
                right=dept_total,
                ok=_near(company_total, dept_total),
            )
        )

        for dept, positions in sorted(by_dept.items()):
            dept_total_m = _sum_metrics(positions, metric)
            by_mg: dict[int, list[PositionSnapshot]] = defaultdict(list)
            for pos in positions:
                by_mg[pos.manager_group_id].append(pos)
            mg_sum = Decimal("0")
            any_mg = False
            for mg_positions in by_mg.values():
                part = _sum_metrics(mg_positions, metric)
                if part is not None:
                    mg_sum += part
                    any_mg = True
            mg_total = mg_sum if any_mg else None
            checks.append(
                ControlEquality(
                    name=f"department_{dept}_{metric}_vs_sum_manager_groups",
                    left=dept_total_m,
                    right=mg_total,
                    ok=_near(dept_total_m, mg_total),
                )
            )
            for mg_id, mg_positions in sorted(by_mg.items()):
                mg_total_m = _sum_metrics(mg_positions, metric)
                by_cp: dict[int, list[PositionSnapshot]] = defaultdict(list)
                for pos in mg_positions:
                    by_cp[pos.counterparty_id].append(pos)
                cp_sum = Decimal("0")
                any_cp = False
                for cp_positions in by_cp.values():
                    part = _sum_metrics(cp_positions, metric)
                    if part is not None:
                        cp_sum += part
                        any_cp = True
                cp_total = cp_sum if any_cp else None
                checks.append(
                    ControlEquality(
                        name=(
                            f"manager_group_{mg_id}_{metric}_vs_sum_counterparties"
                        ),
                        left=mg_total_m,
                        right=cp_total,
                        ok=_near(mg_total_m, cp_total),
                    )
                )

    checks.append(
        ControlEquality(
            name="l2_l4_disclosure_only",
            left=None,
            right=None,
            ok=True,
            diagnostic=True,
        )
    )
    return tuple(checks)


def build_source_file_grand_total_equalities(
    current: Sequence[PositionSnapshot],
    reported_by_source_file: dict[int, dict[str, object | None]],
) -> tuple[ControlEquality, ...]:
    """Re-check stored L1 rows against SourceFile.reported_grand_totals."""
    checks: list[ControlEquality] = []
    l1_by_file: dict[int, list[PositionSnapshot]] = defaultdict(list)
    for pos in current:
        if pos.outline_level == 1:
            l1_by_file[pos.source_file_id].append(pos)

    for source_file_id in sorted(reported_by_source_file):
        reported = reported_by_source_file[source_file_id] or {}
        l1_rows = l1_by_file.get(source_file_id, [])
        for metric in ADDITIVE_METRICS:
            left = _sum_metrics(l1_rows, metric)
            right = _parse_reported_metric(reported.get(metric))
            checks.append(
                ControlEquality(
                    name=(
                        f"source_file_{source_file_id}_{metric}"
                        "_l1_vs_reported_grand_totals"
                    ),
                    left=left,
                    right=right,
                    ok=_near(left, right),
                    diagnostic=False,
                )
            )

        credit_left = _sum_credit_limit(l1_rows)
        credit_right = _parse_reported_metric(reported.get("credit_limit"))
        # Diagnostic only: may diverge; never explains total_debt mismatches.
        checks.append(
            ControlEquality(
                name=(
                    f"source_file_{source_file_id}_credit_limit"
                    "_l1_vs_reported_grand_totals_diagnostic"
                ),
                left=credit_left,
                right=credit_right,
                ok=_near(credit_left, credit_right),
                diagnostic=True,
            )
        )

    return tuple(checks)


async def load_active_reported_grand_totals(
    session: AsyncSession, cycle_id: int
) -> dict[int, dict[str, object | None]]:
    rows = (
        await session.execute(
            select(SourceFile.id, SourceFile.reported_grand_totals).where(
                SourceFile.audit_cycle_id == cycle_id,
                SourceFile.lifecycle_status == SourceFileLifecycle.ACTIVE,
            )
        )
    ).all()
    out: dict[int, dict[str, object | None]] = {}
    for file_id, totals in rows:
        out[int(file_id)] = dict(totals or {})
    return out


async def compare_cycles(
    session: AsyncSession,
    *,
    current_cycle: AuditCycle,
    previous_cycle: AuditCycle | None = None,
) -> CycleComparison:
    if previous_cycle is None:
        previous_cycle = await find_previous_completed_cycle(
            session, current_report_date=current_cycle.report_date
        )

    current_positions = await load_active_positions(session, current_cycle.id)
    previous_positions: list[PositionSnapshot] = []
    if previous_cycle is not None:
        previous_positions = await load_active_positions(session, previous_cycle.id)

    entities, collisions, ambiguous = compare_position_sets(
        current=current_positions, previous=previous_positions
    )
    reported = await load_active_reported_grand_totals(session, current_cycle.id)
    controls = (
        build_l1_control_equalities(current_positions)
        + build_source_file_grand_total_equalities(current_positions, reported)
    )
    return CycleComparison(
        current_cycle_id=current_cycle.id,
        current_report_date=current_cycle.report_date,
        previous_cycle_id=previous_cycle.id if previous_cycle else None,
        previous_report_date=previous_cycle.report_date if previous_cycle else None,
        entities=entities,
        collisions=collisions,
        control_equalities=controls,
        ambiguous_keys=ambiguous,
        current_positions=tuple(current_positions),
        previous_positions=tuple(previous_positions),
    )


@dataclass
class ComparisonSummary:
    """Compact JSON-serializable summary stored on AuditReport.summary_json."""

    previous_cycle_id: int | None
    previous_report_date: str | None
    current_report_date: str
    entity_count: int
    ambiguous_key_count: int
    collision_count: int
    overdue_profile_change_count: int
    new_count: int
    closed_count: int
    control_failures: list[str] = field(default_factory=list)
    company_metrics: dict[str, dict[str, object | None]] = field(default_factory=dict)
    total_overdue: dict[str, object | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "previous_cycle_id": self.previous_cycle_id,
            "previous_report_date": self.previous_report_date,
            "current_report_date": self.current_report_date,
            "entity_count": self.entity_count,
            "ambiguous_key_count": self.ambiguous_key_count,
            "collision_count": self.collision_count,
            "overdue_profile_change_count": self.overdue_profile_change_count,
            "new_count": self.new_count,
            "closed_count": self.closed_count,
            "control_failures": self.control_failures,
            "company_metrics": self.company_metrics,
            "total_overdue": self.total_overdue,
        }


def _metric_delta_payload(delta: MetricDelta) -> dict[str, object | None]:
    return {
        "current": str(delta.current) if delta.current is not None else None,
        "previous": str(delta.previous) if delta.previous is not None else None,
        "abs_delta": str(delta.abs_delta) if delta.abs_delta is not None else None,
        "percent_delta": (
            str(delta.percent_delta) if delta.percent_delta is not None else None
        ),
    }


def _company_metric_deltas(
    comparison: CycleComparison,
) -> dict[str, MetricDelta]:
    curr_l1 = [p for p in comparison.current_positions if p.outline_level == 1]
    prev_l1 = [p for p in comparison.previous_positions if p.outline_level == 1]
    curr_present = bool(curr_l1)
    prev_present = bool(prev_l1)
    out: dict[str, MetricDelta] = {}
    for metric in ADDITIVE_METRICS:
        curr_m = _sum_metrics(curr_l1, metric)
        prev_m = _sum_metrics(prev_l1, metric)
        ad = abs_delta(
            curr_present=curr_present,
            prev_present=prev_present,
            curr_m=curr_m,
            prev_m=prev_m,
        )
        out[metric] = MetricDelta(
            current=curr_m if curr_present else None,
            previous=prev_m if prev_present else None,
            abs_delta=ad,
            percent_delta=percent_delta(ad, prev_m if prev_present else None),
        )
    return out


def _sum_l1_overdue(positions: Sequence[PositionSnapshot]) -> Decimal | None:
    total = Decimal("0")
    any_value = False
    for pos in positions:
        if pos.outline_level != 1:
            continue
        for bucket in OVERDUE_BUCKETS:
            value = pos.metrics.get(bucket)
            if value is None:
                continue
            total += value
            any_value = True
    return total if any_value else None


def summarize_comparison(comparison: CycleComparison) -> ComparisonSummary:
    company = _company_metric_deltas(comparison)
    company_metrics = {
        name: _metric_delta_payload(company[name]) for name in ADDITIVE_METRICS
    }

    curr_od = _sum_l1_overdue(comparison.current_positions)
    prev_od = _sum_l1_overdue(comparison.previous_positions)
    curr_present = any(p.outline_level == 1 for p in comparison.current_positions)
    prev_present = any(p.outline_level == 1 for p in comparison.previous_positions)
    od_abs = abs_delta(
        curr_present=curr_present,
        prev_present=prev_present,
        curr_m=curr_od,
        prev_m=prev_od,
    )
    od_pct = percent_delta(od_abs, prev_od if prev_present else None)
    total_overdue = {
        "current": str(curr_od) if curr_od is not None else None,
        "previous": str(prev_od) if prev_od is not None else None,
        "abs_delta": str(od_abs) if od_abs is not None else None,
        "percent_delta": str(od_pct) if od_pct is not None else None,
    }

    new_count = sum(
        1
        for e in comparison.entities
        if not e.ambiguous and e.current is not None and e.previous is None
    )
    closed_count = sum(
        1
        for e in comparison.entities
        if not e.ambiguous and e.current is None and e.previous is not None
    )

    return ComparisonSummary(
        previous_cycle_id=comparison.previous_cycle_id,
        previous_report_date=(
            comparison.previous_report_date.isoformat()
            if comparison.previous_report_date
            else None
        ),
        current_report_date=comparison.current_report_date.isoformat(),
        entity_count=len(comparison.entities),
        ambiguous_key_count=len(comparison.ambiguous_keys),
        collision_count=len(comparison.collisions),
        overdue_profile_change_count=sum(
            1 for e in comparison.entities if e.overdue_profile_changed
        ),
        new_count=new_count,
        closed_count=closed_count,
        control_failures=[
            c.name for c in comparison.control_equalities if not c.ok and not c.diagnostic
        ],
        company_metrics=company_metrics,
        total_overdue=total_overdue,
    )
