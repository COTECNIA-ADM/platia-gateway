"""Registro de cambios hechos desde el panel (G13, ADR-017).

Una fila por cada cambio de configuración: quién, cuándo, desde dónde, qué acción, sobre qué
y los valores nuevos. Nunca lleva secretos (claves, contraseñas, secretos de firma): solo que
se cambiaron. Como la auditoría, guarda ids como enteros sin claves foráneas, para que
sobreviva a cambios en lo que describe.
"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class AdminAction(Base):
    __tablename__ = "admin_actions"
    __table_args__ = (Index("ix_admin_actions_org_created", "organization_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Nulo: cambio del catálogo común o de un webhook global (lo hace quien opera el gateway)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # p. ej. "application.create", "limit.delete", "provider.rotate_key"
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
