"""`Counterparty` — см. `docs/DATA_CONTRACT.md` §7.

Идентичность контрагента — `manager_group_id + normalized_name`, а не глобальное
имя: одноимённые контрагенты у разных `ManagerGroup` — независимые сущности
(бизнес-правило подтверждено Александром, вопрос закрыт). Каноническая
(не per-файловая) сущность — та же логика стабильности, что и у `ManagerGroup`.
"""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from app.domain.models.manager_group import ManagerGroup


class Counterparty(Base):
    __tablename__ = "counterparties"
    __table_args__ = (
        UniqueConstraint("manager_group_id", "normalized_name", name="uq_counterparty_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    manager_group_id: Mapped[int] = mapped_column(ForeignKey("manager_groups.id"), nullable=False)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    manager_group: Mapped["ManagerGroup"] = relationship(back_populates="counterparties")
