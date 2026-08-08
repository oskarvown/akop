"""`DebtPosition` — строка уровня 1–4 (контрагент/договор/объект/документ).

См. `docs/DATA_CONTRACT.md` §3 (outline levels), §3.1 (состав колонок), §9
(иерархия для отчёта). Уровень 0 (`ManagerGroup`) — отдельная сущность
(`ManagerGroup`), не строка `DebtPosition`.

Иерархия 1→2→3→4 хранится через самоссылающийся `parent_position_id`
(родитель уровня 1 — `None`, у него нет строки-предка `DebtPosition`, только
`manager_group_id`). Уровни 2–4 физически сохраняются (не отбрасываются), но
`app/domain/calculations/reconciliation.py` учитывает в агрегатах только
`outline_level == 1`, чтобы избежать двойного счёта (§3, §9).

`payment_deferral_days: int | null` — неаддитивная метрика уровня записи
(§6.1): ошибка значения не блокирует файл целиком, а фиксируется в
`payment_deferral_error` для конкретной строки.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from app.domain.models.counterparty import Counterparty
    from app.domain.models.manager_group import ManagerGroup
    from app.domain.models.source_file import SourceFile

MONEY = Numeric(18, 2)


class DebtPosition(Base):
    __tablename__ = "debt_positions"
    __table_args__ = (
        CheckConstraint("outline_level BETWEEN 1 AND 4", name="ck_debt_position_outline_level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    manager_group_id: Mapped[int] = mapped_column(ForeignKey("manager_groups.id"), nullable=False)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparties.id"), nullable=False)
    parent_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("debt_positions.id"), nullable=True
    )

    outline_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    row_order: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_label: Mapped[str] = mapped_column(Text, nullable=False)
    # Stage 4.1 — cross-cycle matching (row_order is never identity)
    normalized_label: Mapped[str] = mapped_column(Text, nullable=False)
    match_key: Mapped[str] = mapped_column(Text, nullable=False)
    match_key_hash: Mapped[str] = mapped_column(Text, nullable=False)

    payment_deferral_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_deferral_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    credit_limit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    document_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    total_debt: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    advance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    not_due: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    overdue_1_7: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    overdue_8_14: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    overdue_15_21: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    overdue_22_30: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    overdue_over_31: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    comment_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_file: Mapped["SourceFile"] = relationship(back_populates="debt_positions")
    manager_group: Mapped["ManagerGroup"] = relationship()
    counterparty: Mapped["Counterparty"] = relationship()
    parent: Mapped["DebtPosition | None"] = relationship(remote_side="DebtPosition.id")
