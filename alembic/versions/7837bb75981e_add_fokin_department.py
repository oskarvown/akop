"""add fokin department

Пятый отдел «Фокин» (один менеджер) добавлен наравне с остальными четырьмя
по бизнес-решению — см. `docs/DATA_CONTRACT.md` §2, §2.4.

`ALTER TYPE ... ADD VALUE` выполняется через `autocommit_block()`: эта
команда не может участвовать в обычной транзакции Alembic на некоторых
версиях PostgreSQL (до PG12 — вообще нельзя внутри транзакции; на PG12+
можно, но не в одной транзакции с последующим использованием нового
значения) — см. официальный рецепт Alembic для добавления значений ENUM.

Revision ID: 7837bb75981e
Revises: 7d9601156933
Create Date: 2026-07-30 19:10:39.435967

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7837bb75981e'
down_revision: Union[str, None] = '7d9601156933'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ("SZFO_1", "SZFO_2", "REGIONAL", "MOSCOW")
_NEW_VALUE = "FOKIN"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE department ADD VALUE IF NOT EXISTS '{_NEW_VALUE}'")


def downgrade() -> None:
    # Postgres не поддерживает удаление значения ENUM напрямую — пересоздаём
    # тип без 'FOKIN'. Если в БД есть строки с department='FOKIN' (в
    # manager_groups или source_files), downgrade упадёт на ALTER TABLE ...
    # USING — это ожидаемо: перед откатом миграции нужно вручную удалить или
    # перепривязать данные отдела «Фокин».
    bind = op.get_bind()

    op.execute("ALTER TYPE department RENAME TO department_old")

    new_department_enum = sa.Enum(*_OLD_VALUES, name="department")
    new_department_enum.create(bind, checkfirst=False)

    for table in ("manager_groups", "source_files"):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN department TYPE department "
            f"USING department::text::department"
        )

    op.execute("DROP TYPE department_old")
