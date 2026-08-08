"""Unit tests for Stage 4.3 report delivery formatting and args."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.application.comparison_service import (
    ADDITIVE_METRICS,
    CycleComparison,
    ControlEquality,
    summarize_comparison,
)
from app.application.report_delivery_service import (
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    build_report_filename,
    format_report_caption,
    format_report_summary_messages,
)
from app.bot.handlers.report import ReportArgsError, parse_report_args
from app.domain.models import AuditArtifactKind


def test_parse_report_args_strict() -> None:
    assert parse_report_args(None) == parse_report_args("")
    assert parse_report_args("").force_core is False
    assert parse_report_args("").report_date is None
    parsed = parse_report_args("2026-08-08")
    assert parsed.report_date == dt.date(2026, 8, 8)
    assert parsed.force_core is False
    core = parse_report_args("core")
    assert core.force_core is True
    assert core.report_date is None
    both = parse_report_args("core 2026-08-08")
    assert both.force_core is True
    assert both.report_date == dt.date(2026, 8, 8)
    for bad in ("foo", "core foo", "2026-8-8", "core 2026-08-08 extra", "report"):
        with pytest.raises(ReportArgsError):
            parse_report_args(bad)


@pytest.mark.parametrize(
    "args",
    ("2026-02-30", "2026-13-01", "core 2026-02-30"),
)
def test_parse_report_args_rejects_impossible_dates(args: str) -> None:
    with pytest.raises(ReportArgsError):
        parse_report_args(args)


def test_filename_and_caption_deterministic() -> None:
    assert (
        build_report_filename(
            report_date=dt.date(2026, 8, 8), kind=AuditArtifactKind.CORE
        )
        == "Дебиторка_2026-08-08_CORE.xlsx"
    )
    assert (
        build_report_filename(
            report_date=dt.date(2026, 8, 8),
            kind=AuditArtifactKind.ENRICHED,
            revision=3,
        )
        == "Дебиторка_2026-08-08_ENRICHED_r3.xlsx"
    )
    caption = format_report_caption(
        report_date=dt.date(2026, 8, 8), kind=AuditArtifactKind.CORE
    )
    assert len(caption) <= TELEGRAM_CAPTION_LIMIT
    assert "CORE" in caption


def test_summary_formatter_debt_wording_and_legacy() -> None:
    rich = {
        "company_metrics": {
            "total_debt": {
                "current": "100.00",
                "previous": "150.00",
                "abs_delta": "-50.00",
                "percent_delta": "-33.33",
            }
        },
        "total_overdue": {"current": "10.00"},
        "new_count": 1,
        "closed_count": 2,
        "overdue_profile_change_count": 0,
        "control_failures": [],
    }
    text = "\n".join(format_report_summary_messages(rich))
    assert "чистое снижение долга" in text
    assert "оплат" not in text.lower()
    assert "платёж" not in text.lower()

    growth = {
        "company_metrics": {
            "total_debt": {
                "current": "200",
                "previous": "100",
                "abs_delta": "100",
                "percent_delta": "100",
            }
        }
    }
    assert "рост долга" in "\n".join(format_report_summary_messages(growth))

    legacy = format_report_summary_messages({"entity_count": 3})
    assert legacy
    assert "приложенном файле" in legacy[0]

    baseline = format_report_summary_messages(
        {
            "company_metrics": {
                "total_debt": {
                    "current": "10",
                    "previous": None,
                    "abs_delta": "10",
                    "percent_delta": None,
                }
            }
        }
    )
    joined = "\n".join(baseline)
    assert "Текущий долг" in joined
    assert "Сравнение с предыдущим периодом: нет данных" in joined
    assert "рост" not in joined.lower()
    assert "снижение" not in joined.lower()


def test_summary_messages_respect_telegram_limit() -> None:
    huge = {
        "company_metrics": {
            name: {
                "current": "1",
                "previous": "0",
                "abs_delta": "1",
                "percent_delta": None,
            }
            for name in ADDITIVE_METRICS
        },
        "control_failures": ["x" * 5000],
    }
    messages = format_report_summary_messages(huge)
    assert all(len(m) <= TELEGRAM_MESSAGE_LIMIT for m in messages)
    resumed = format_report_summary_messages(huge, start_index=1)
    assert resumed == messages[1:]


def test_summarize_comparison_includes_nine_company_metrics() -> None:
    from app.application.comparison_service import PositionSnapshot

    def _pos(pid: int, total: str) -> PositionSnapshot:
        metrics = {m: Decimal("0") for m in ADDITIVE_METRICS}
        metrics["total_debt"] = Decimal(total)
        metrics["overdue_1_7"] = Decimal("1")
        return PositionSnapshot(
            id=pid,
            match_key=f"c:{pid}",
            match_key_hash="h" * 64,
            outline_level=1,
            raw_label="X",
            counterparty_id=pid,
            manager_group_id=1,
            source_file_id=1,
            department="regional",
            metrics=metrics,
            credit_limit=None,
        )

    comparison = CycleComparison(
        current_cycle_id=2,
        current_report_date=dt.date(2026, 8, 8),
        previous_cycle_id=1,
        previous_report_date=dt.date(2026, 8, 1),
        entities=(),
        collisions=(),
        control_equalities=(
            ControlEquality(
                name="company_total_debt_vs_sum_departments",
                left=Decimal("100"),
                right=Decimal("100"),
                ok=True,
            ),
        ),
        ambiguous_keys=frozenset(),
        current_positions=(_pos(1, "100"),),
        previous_positions=(_pos(2, "40"),),
    )
    summary = summarize_comparison(comparison).as_dict()
    assert summary["current_report_date"] == "2026-08-08"
    assert set(summary["company_metrics"]) == set(ADDITIVE_METRICS)
    for metric in ADDITIVE_METRICS:
        payload = summary["company_metrics"][metric]
        assert set(payload) == {"current", "previous", "abs_delta", "percent_delta"}
    assert summary["company_metrics"]["total_debt"]["current"] == "100"
    assert summary["total_overdue"]["current"] == "1"
