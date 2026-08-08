"""Unit tests for idle / reminder / expire policy."""
from __future__ import annotations

import datetime as dt

from app.application.idle_policy import IdleAction, IdleTimeouts, decide_idle_action

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
TIMEOUTS = IdleTimeouts(
    idle_seconds=3600,
    reminder_interval_seconds=3600,
    max_reminders=2,
    expire_grace_seconds=86400,
)


def test_no_premature_first_reminder() -> None:
    action = decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(seconds=3599),
        reminder_count=0,
        last_reminder_at=None,
        timeouts=TIMEOUTS,
    )
    assert action == IdleAction.SKIP


def test_first_reminder_after_idle() -> None:
    action = decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(seconds=3600),
        reminder_count=0,
        last_reminder_at=None,
        timeouts=TIMEOUTS,
    )
    assert action == IdleAction.REMIND


def test_no_premature_second_reminder() -> None:
    action = decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(days=2),
        reminder_count=1,
        last_reminder_at=NOW - dt.timedelta(seconds=3599),
        timeouts=TIMEOUTS,
    )
    assert action == IdleAction.SKIP


def test_second_reminder_after_interval() -> None:
    action = decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(days=2),
        reminder_count=1,
        last_reminder_at=NOW - dt.timedelta(seconds=3600),
        timeouts=TIMEOUTS,
    )
    assert action == IdleAction.REMIND


def test_grace_before_expire_after_max_reminders() -> None:
    action = decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(days=10),
        reminder_count=2,
        last_reminder_at=NOW - dt.timedelta(hours=23),
        timeouts=TIMEOUTS,
    )
    assert action == IdleAction.SKIP


def test_expire_after_max_and_grace() -> None:
    action = decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(days=10),
        reminder_count=2,
        last_reminder_at=NOW - dt.timedelta(seconds=86400),
        timeouts=TIMEOUTS,
    )
    assert action == IdleAction.EXPIRE


def test_expire_never_means_completed() -> None:
    assert IdleAction.EXPIRE != "completed"
    assert decide_idle_action(
        now_utc=NOW,
        last_activity_at=NOW - dt.timedelta(days=10),
        reminder_count=2,
        last_reminder_at=NOW - dt.timedelta(days=2),
        timeouts=TIMEOUTS,
    ) == IdleAction.EXPIRE
