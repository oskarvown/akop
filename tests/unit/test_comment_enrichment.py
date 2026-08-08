"""Unit tests for Stage 4.4 comment parser and enrichment hashing."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.application.comment_enrichment_service import (
    compute_analysis_input_hash,
    compute_enrichment_input_hash,
)
from app.domain.calculations.comment_parser import (
    CommentParseOutcome,
    apply_report_year_rule,
    parse_comment,
)

_VERSIONS = {
    "parser_version": "1",
    "prompt_version": "1",
    "schema_version_llm": "1",
    "redaction_version": "1",
    "model_name": "m",
}


def _snapshot(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "debt_position_id": 1,
        "source_file_id": 1,
        "row_order": 1,
        "department": "regional",
        "manager_group": "m",
        "counterparty_label": "c",
        "outline_level": 1,
        "comment_raw": "x",
    }
    base.update(overrides)
    return base


def test_year_without_year_uses_report_date_and_rollover() -> None:
    report = dt.date(2026, 8, 8)
    assert apply_report_year_rule(10, 8, None, report_date=report) == dt.date(
        2026, 8, 10
    )
    assert apply_report_year_rule(1, 8, None, report_date=report) == dt.date(
        2027, 8, 1
    )


def test_parse_comment_resolved_and_ambiguous() -> None:
    report = dt.date(2026, 8, 8)
    resolved = parse_comment("Оплата 15.08 на 10 000 руб", report_date=report)
    assert resolved.outcome is CommentParseOutcome.RESOLVED
    assert resolved.mentioned_date == dt.date(2026, 8, 15)
    assert resolved.mentioned_amount == Decimal("10000.0")
    assert resolved.action == "оплата"

    ambiguous = parse_comment("уточнить позже", report_date=report)
    assert ambiguous.outcome is CommentParseOutcome.AMBIGUOUS

    empty = parse_comment("  ", report_date=report)
    assert empty.outcome is CommentParseOutcome.EMPTY


def test_analysis_hash_differs_by_report_date() -> None:
    snap = _snapshot()
    a = compute_analysis_input_hash(
        snapshot=snap, report_date=dt.date(2026, 1, 1), versions=_VERSIONS
    )
    b = compute_analysis_input_hash(
        snapshot=snap, report_date=dt.date(2026, 1, 2), versions=_VERSIONS
    )
    assert a != b


def test_analysis_hash_differs_by_counterparty_label() -> None:
    a = compute_analysis_input_hash(
        snapshot=_snapshot(counterparty_label="Alpha"),
        report_date=dt.date(2026, 1, 1),
        versions=_VERSIONS,
    )
    b = compute_analysis_input_hash(
        snapshot=_snapshot(counterparty_label="Beta"),
        report_date=dt.date(2026, 1, 1),
        versions=_VERSIONS,
    )
    assert a != b


def test_enrichment_hash_includes_report_date() -> None:
    snap = [_snapshot()]
    h1 = compute_enrichment_input_hash(
        snapshot=snap,
        report_date=dt.date(2026, 1, 1),
        financial_input_hash="abc",
        versions=_VERSIONS,
    )
    h2 = compute_enrichment_input_hash(
        snapshot=snap,
        report_date=dt.date(2026, 1, 2),
        financial_input_hash="abc",
        versions=_VERSIONS,
    )
    assert h1 != h2
