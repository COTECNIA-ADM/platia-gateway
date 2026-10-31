"""Base declarativa y columnas comunes.

Las fechas se guardan en UTC sin zona (DATETIME de MySQL): `utcnow()` es la única
fuente de "ahora" para los valores por defecto.
"""
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Nombres de restricciones estables: Alembic los necesita para poder borrarlas
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    """Borrado lógico: la configuración no se borra para que la auditoría siga teniendo sentido."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
