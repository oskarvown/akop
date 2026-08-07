"""`ManagerGroup` — см. `docs/DATA_CONTRACT.md` §2.2, §2.3.

Каноническая (не per-файловая) сущность: одна и та же пара
`(department, normalized_name)` всегда разрешается в один и тот же `id`,
независимо от того, в каком `SourceFile`/аудите она впервые встретилась
(get-or-create — см. `app/infrastructure/excel/persistence.py`). Это уже
реализует требование «стабильная идентичность между аудитами» на уровне схемы,
без необходимости отдельной таблицы `AuditCycle` (Stage 3).
"""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import Department
from app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from app.domain.models.counterparty import Counterparty


class ManagerGroup(Base):
    __tablename__ = "manager_groups"
    __table_args__ = (
        UniqueConstraint("department", "normalized_name", name="uq_manager_group_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    department: Mapped[Department] = mapped_column(Enum(Department, name="department"), nullable=False)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    counterparties: Mapped[list["Counterparty"]] = relationship(back_populates="manager_group")
