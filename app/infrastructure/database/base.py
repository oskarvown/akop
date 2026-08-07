"""Базовый класс декларативных моделей SQLAlchemy.

Конкретные доменные модели (SourceFile, AuditCycle, ManagerGroup, Counterparty и т.д.)
создаются на Stage 2+ по контракту `docs/DATA_CONTRACT.md`. Stage 1 фиксирует только
инфраструктуру подключения.
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Общий предок для всех ORM-моделей приложения."""
