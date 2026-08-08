"""Unit-тесты safety guard для destructive cleanup integration-тестов."""
from __future__ import annotations

import pytest

from tests.integration.db_safety import (
    DestructiveCleanupForbiddenError,
    assert_destructive_cleanup_allowed,
    is_destructive_cleanup_allowed,
)


@pytest.fixture(autouse=True)
def _isolate_destructive_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_TEST_DB", raising=False)


@pytest.mark.parametrize(
    ("db_name", "override", "expected"),
    [
        ("debitor_bot_test", None, True),
        ("app_test", None, True),
        ("debitor_bot", None, False),
        ("debitor_bot", "1", True),
        ("debitor_bot", "0", False),
        ("debitor_bot", "", False),
        ("debitor_bot_test", "0", True),
    ],
)
def test_is_destructive_cleanup_allowed(
    db_name: str,
    override: str | None,
    expected: bool,
) -> None:
    assert is_destructive_cleanup_allowed(db_name, allow_override=override) is expected


def test_assert_destructive_cleanup_allowed_accepts_test_db_suffix() -> None:
    assert_destructive_cleanup_allowed("debitor_bot_test")


def test_assert_destructive_cleanup_allowed_accepts_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_TEST_DB", "1")
    assert_destructive_cleanup_allowed("debitor_bot")


def test_assert_destructive_cleanup_allowed_rejects_production_like_db() -> None:
    with pytest.raises(DestructiveCleanupForbiddenError, match="debitor_bot") as exc_info:
        assert_destructive_cleanup_allowed("debitor_bot")
    assert "_test" in str(exc_info.value)
    assert "ALLOW_DESTRUCTIVE_TEST_DB=1" in str(exc_info.value)
