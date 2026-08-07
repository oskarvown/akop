# Дебиторка-бот

Закрытый Telegram-бот для еженедельного аудита дебиторской задолженности (Ikaplast).
Полный контекст и решения — в `docs/IMPLEMENTATION_PLAN.md`, `docs/DATA_CONTRACT.md`,
`docs/ASSUMPTIONS.md`, `docs/REQUIREMENTS_TRACEABILITY.md`.

Развёртывание — **без Docker** (см. `docs/ASSUMPTIONS.md` §2.3): `venv` + `systemd` +
PostgreSQL.

## Требования

- Python 3.11+
- PostgreSQL 14+ (локально или managed)

## Локальная разработка

### PostgreSQL (без Docker, macOS + Homebrew)

```bash
brew install postgresql@16
brew services start postgresql@16   # аналог systemd для macOS, тоже не Docker

# создать роль и БД (пароль подставить свой, не хранить в docs/истории команд)
psql -d postgres -c "CREATE ROLE debitor_bot LOGIN PASSWORD '<пароль>';"
psql -d postgres -c "CREATE DATABASE debitor_bot OWNER debitor_bot;"
```

На production-сервере (Linux) — нативный пакет дистрибутива (`apt install postgresql`
и т.п.) и `systemctl enable --now postgresql`, тоже без Docker (см. `docs/ASSUMPTIONS.md` §2.3).

### Приложение

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# заполнить BOT_TOKEN, ALLOWED_USER_ID, DB_* (значения роли/БД из шага выше),
# AUDIT_IDLE_TIMEOUT_SECONDS

# применить миграции (после появления доменных моделей на Stage 2+)
alembic upgrade head

# запустить бота (polling)
python -m app.main
```

## Тесты

```bash
pytest
```

## Production-деплой (systemd, без контейнеров)

1. Скопировать код в `/opt/debitor-bot`, создать `venv` и установить `requirements.txt`
   (без dev-зависимостей).
2. Заполнить `/opt/debitor-bot/.env` реальными значениями.
3. Применить миграции: `alembic upgrade head`.
4. Установить unit: `sudo cp scripts/deploy/debitor-bot.service /etc/systemd/system/`.
5. `sudo systemctl daemon-reload && sudo systemctl enable --now debitor-bot`.
6. Резервное копирование PostgreSQL — cron + `pg_dump` (настраивается на Stage 7).

## Структура репозитория

```text
app/
  bot/{handlers,keyboards,middlewares,messages}/
  application/        # сервисы прикладного уровня (Stage 3+)
  domain/{models,enums,calculations,matching}/
  infrastructure/{database,excel,telegram,llm,storage}/
  config/             # pydantic-settings
  main.py             # точка входа
alembic/              # миграции БД
tests/{unit,integration,fixtures,acceptance}/
docs/                 # Stage 0 контракты и допущения
scripts/deploy/        # systemd unit
```
