"""Application configuration.

Все значения читаются только из окружения/`.env` (см. `docs/ASSUMPTIONS.md` §4).
Ничего не хардкодится, включая тайм-ауты и допуски.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Понятная ошибка конфигурации: не заданы обязательные переменные окружения.

    Оборачивает `pydantic.ValidationError`, чтобы вместо низкоуровневого
    pydantic-traceback пользователь/оператор сразу видел, каких именно
    переменных не хватает и что с этим делать.
    """


class Settings(BaseSettings):
    """Единая точка чтения конфигурации приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    bot_token: str = Field(..., alias="BOT_TOKEN")
    # Comma-separated Telegram user ids; parsed via `allowed_user_ids` property.
    # Kept as str so pydantic-settings does not JSON-decode the dotenv value.
    allowed_user_ids_raw: str = Field(..., alias="ALLOWED_USER_IDS")
    max_upload_size_bytes: int = Field(
        20 * 1024 * 1024,
        alias="MAX_UPLOAD_SIZE_BYTES",
    )

    # PostgreSQL
    db_host: str = Field("localhost", alias="DB_HOST")
    db_port: int = Field(5432, alias="DB_PORT")
    db_name: str = Field(..., alias="DB_NAME")
    db_user: str = Field(..., alias="DB_USER")
    db_password: str = Field(..., alias="DB_PASSWORD")

    # Недельный цикл (Stage 3) — не хардкодится, см. docs/IMPLEMENTATION_PLAN.md
    audit_idle_timeout_seconds: int = Field(..., alias="AUDIT_IDLE_TIMEOUT_SECONDS")

    # LLM fallback (Roadmap §6.1, провайдер не выбран — Stage 6)
    llm_api_key: str | None = Field(None, alias="LLM_API_KEY")
    llm_model: str | None = Field(None, alias="LLM_MODEL")

    @field_validator("allowed_user_ids_raw")
    @classmethod
    def _non_empty_allowed_user_ids(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ALLOWED_USER_IDS must contain at least one id")
        # Fail fast on malformed tokens.
        _ = _parse_allowed_user_ids(value)
        return value

    @field_validator("audit_idle_timeout_seconds", "max_upload_size_bytes")
    @classmethod
    def _positive_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("AUDIT_IDLE_TIMEOUT_SECONDS must be positive")
        return value

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        return _parse_allowed_user_ids(self.allowed_user_ids_raw)

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy DSN (asyncpg driver)."""
        dsn = PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            path=self.db_name,
        )
        return str(dsn)


def _parse_allowed_user_ids(value: str) -> frozenset[int]:
    parts = [
        part.strip()
        for part in value.replace(";", ",").split(",")
        if part.strip()
    ]
    if not parts:
        raise ValueError("ALLOWED_USER_IDS must contain at least one id")
    try:
        return frozenset(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(
            "ALLOWED_USER_IDS must be a comma-separated list of integers"
        ) from exc


def _build_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing: set[str] = set()
        for error in exc.errors():
            if error.get("type") != "missing" or not error.get("loc"):
                continue
            field_name = str(error["loc"][0])
            field = Settings.model_fields.get(field_name)
            missing.add(str(field.alias) if field is not None and field.alias else field_name)
        details = ", ".join(sorted(missing)) if missing else str(exc)
        raise ConfigurationError(
            "Не заданы обязательные переменные окружения: "
            f"{details}. Скопируйте .env.example в .env и заполните значения "
            "(см. docs/ASSUMPTIONS.md §4)."
        ) from exc


@lru_cache
def get_settings() -> Settings:
    """Кэшированный singleton настроек (читается один раз за процесс)."""
    return _build_settings()
