"""Stage 1 sanity-тест: конфигурация читается из окружения и строит корректный DSN."""
import pytest

from app.config.settings import ConfigurationError, Settings, get_settings

_REQUIRED_ENV_VARS = (
    "BOT_TOKEN",
    "ALLOWED_USER_IDS",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "AUDIT_IDLE_TIMEOUT_SECONDS",
    "AUDIT_REMINDER_INTERVAL_SECONDS",
    "AUDIT_MAX_REMINDERS",
    "AUDIT_EXPIRE_GRACE_SECONDS",
    "AUDIT_NOTIFICATION_CHAT_ID",
    "AUDIT_SCHEDULER_POLL_SECONDS",
    "AUDIT_REMINDER_CLAIM_TTL_SECONDS",
    "AUDIT_REMINDER_SEND_TIMEOUT_SECONDS",
    "AUDIT_REMINDER_ERROR_BACKOFF_SECONDS",
    "REPORT_BUILD_CLAIM_TTL_SECONDS",
    "REPORT_BUILD_MAX_ATTEMPTS",
    "REPORT_BUILD_BACKOFF_SECONDS",
    "REPORT_SCHEDULER_POLL_SECONDS",
    "REPORT_DELIVERY_CLAIM_TTL_SECONDS",
    "REPORT_DELIVERY_SEND_TIMEOUT_SECONDS",
    "REPORT_DELIVERY_MAX_ATTEMPTS",
    "REPORT_DELIVERY_BACKOFF_SECONDS",
    "REPORT_DELIVERY_MAX_FILE_BYTES",
    "REPORT_DELIVERY_BATCH_SIZE",
)

_STAGE32_DEFAULTS = {
    "AUDIT_REMINDER_INTERVAL_SECONDS": "86400",
    "AUDIT_MAX_REMINDERS": "2",
    "AUDIT_EXPIRE_GRACE_SECONDS": "86400",
    "AUDIT_NOTIFICATION_CHAT_ID": "743971617",
    "AUDIT_SCHEDULER_POLL_SECONDS": "60",
    "AUDIT_REMINDER_CLAIM_TTL_SECONDS": "300",
    "AUDIT_REMINDER_SEND_TIMEOUT_SECONDS": "30",
    "AUDIT_REMINDER_ERROR_BACKOFF_SECONDS": "900",
    "REPORT_BUILD_CLAIM_TTL_SECONDS": "300",
    "REPORT_BUILD_MAX_ATTEMPTS": "5",
    "REPORT_BUILD_BACKOFF_SECONDS": "60",
    "REPORT_SCHEDULER_POLL_SECONDS": "30",
    "REPORT_DELIVERY_CLAIM_TTL_SECONDS": "300",
    "REPORT_DELIVERY_SEND_TIMEOUT_SECONDS": "30",
    "REPORT_DELIVERY_MAX_ATTEMPTS": "5",
    "REPORT_DELIVERY_BACKOFF_SECONDS": "60",
    "REPORT_DELIVERY_MAX_FILE_BYTES": "52428800",
    "REPORT_DELIVERY_BATCH_SIZE": "10",
}


def _set_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "42, 77")
    monkeypatch.setenv("DB_HOST", "db.local")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "debitor_bot")
    monkeypatch.setenv("DB_USER", "debitor_bot")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("AUDIT_IDLE_TIMEOUT_SECONDS", "86400")
    for key, value in _STAGE32_DEFAULTS.items():
        monkeypatch.setenv(key, value)


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_base_env(monkeypatch)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.bot_token == "test-token"
    assert settings.allowed_user_ids == frozenset({42, 77})
    assert settings.audit_idle_timeout_seconds == 86400
    assert settings.audit_max_reminders == 2
    assert settings.audit_notification_chat_id == 743971617
    assert settings.audit_reminder_send_timeout_seconds < (
        settings.audit_reminder_claim_ttl_seconds
    )
    assert settings.database_url == (
        "postgresql+asyncpg://debitor_bot:secret@db.local:5432/debitor_bot"
    )


def test_settings_rejects_non_positive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_base_env(monkeypatch)
    monkeypatch.setenv("AUDIT_IDLE_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_rejects_send_timeout_not_less_than_claim_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_env(monkeypatch)
    monkeypatch.setenv("AUDIT_REMINDER_SEND_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("AUDIT_REMINDER_CLAIM_TTL_SECONDS", "300")

    with pytest.raises(ValueError, match="SEND_TIMEOUT"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_rejects_delivery_send_timeout_not_less_than_claim_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REPORT_DELIVERY_SEND_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("REPORT_DELIVERY_CLAIM_TTL_SECONDS", "300")

    with pytest.raises(ValueError, match="REPORT_DELIVERY_SEND_TIMEOUT"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_reads_delivery_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_base_env(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.report_delivery_batch_size == 10
    assert settings.report_delivery_max_file_bytes == 52428800
    assert settings.report_delivery_send_timeout_seconds < (
        settings.report_delivery_claim_ttl_seconds
    )


def test_missing_required_env_vars_raise_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in _REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("DB_NAME", "debitor_bot")

    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            get_settings()
    finally:
        get_settings.cache_clear()

    message = str(exc_info.value)
    assert "ALLOWED_USER_IDS" in message
    assert "DB_USER" in message
    assert "DB_PASSWORD" in message
    assert "AUDIT_IDLE_TIMEOUT_SECONDS" in message
    assert "AUDIT_NOTIFICATION_CHAT_ID" in message
    assert "BOT_TOKEN" not in message
