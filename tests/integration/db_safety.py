"""Safety guard для destructive cleanup в PostgreSQL integration/e2e-тестах."""
from __future__ import annotations

import os


class DestructiveCleanupForbiddenError(RuntimeError):
    """Integration-тест пытается очистить БД, не помеченную как тестовая."""


def is_destructive_cleanup_allowed(
    db_name: str,
    *,
    allow_override: str | None = None,
) -> bool:
    """Разрешить DELETE/TRUNCATE только для *_test или явного override."""
    if db_name.endswith("_test"):
        return True
    override = (
        allow_override
        if allow_override is not None
        else os.environ.get("ALLOW_DESTRUCTIVE_TEST_DB")
    )
    return override == "1"


def assert_destructive_cleanup_allowed(db_name: str) -> None:
    """Проверить имя БД перед destructive cleanup; иначе — немедленный отказ."""
    if is_destructive_cleanup_allowed(db_name):
        return
    raise DestructiveCleanupForbiddenError(
        "Destructive cleanup integration-тестов запрещён для БД "
        f"{db_name!r}: имя должно оканчиваться на '_test' "
        "или нужно явно задать ALLOW_DESTRUCTIVE_TEST_DB=1 в окружении процесса pytest. "
        "Не добавляйте override в production .env."
    )
