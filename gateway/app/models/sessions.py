"""Sesiones de las personas en el panel y el portal (G10, ADR-014).

Una fila por inicio de sesión. La renovación (refresh) es opaca: ``<id>.<secreto>``, y aquí
solo se guarda el hash del secreto vigente. Cada renovación lo cambia; si llega uno
anterior, alguien lo ha copiado y la sesión entera se revoca.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    refresh_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # Caduca si no se renueva a tiempo (se alarga en cada renovación) y siempre en max_expires_at
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    max_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # logout, revoked, password_changed, mfa_changed, refresh_reused, admin
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Para que la persona reconozca sus sesiones en el portal
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
