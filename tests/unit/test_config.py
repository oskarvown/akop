"""Stage 1 sanity-тест: конфигурация читается из окружения и строит корректный DSN."""
import pytest

from app.config.settings import ConfigurationError, Settings, get_settings

_REQUIRED_ENV_VARS = (
    "BOT_TOKEN",
    "ALLOWED_USER_ID",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "AUDIT_IDLE_TIMEOUT_SECONDS",
)


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DB_HOST", "db.local")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "debitor_bot")
    monkeypatch.setenv("DB_USER", "debitor_bot")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("AUDIT_IDLE_TIMEOUT_SECONDS", "1800")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.bot_token == "test-token"
    assert settings.allowed_user_id == 42
    assert settings.audit_idle_timeout_seconds == 1800
    assert settings.database_url == (
        "postgresql+asyncpg://debitor_bot:secret@db.local:5432/debitor_bot"
    )


def test_settings_rejects_non_positive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DB_NAME", "debitor_bot")
    monkeypatch.setenv("DB_USER", "debitor_bot")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("AUDIT_IDLE_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_missing_required_env_vars_raise_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Отсутствие обязательных переменных должно давать понятную ошибку конфигурации,
    а не низкоуровневый pydantic-traceback.

    `get_settings()`/`Settings()` по умолчанию читают `.env` из текущей рабочей
    директории (см. `model_config.env_file` в `app/config/settings.py`) — тест
    переключается в пустую временную директорию, чтобы реальный `.env`
    разработчика (не коммитится, но физически присутствует локально) не
    подмешивал значения и не маскировал отсутствие переменных окружения.
    """
    monkeypatch.chdir(tmp_path)
    for var in _REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("DB_NAME", "debitor_bot")
    # ALLOWED_USER_ID, DB_USER, DB_PASSWORD, AUDIT_IDLE_TIMEOUT_SECONDS намеренно не заданы.

    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            get_settings()
    finally:
        get_settings.cache_clear()

    message = str(exc_info.value)
    assert "ALLOWED_USER_ID" in message
    assert "DB_USER" in message
    assert "DB_PASSWORD" in message
    assert "AUDIT_IDLE_TIMEOUT_SECONDS" in message
    assert "BOT_TOKEN" not in message  # он был задан — не должен считаться отсутствующим
