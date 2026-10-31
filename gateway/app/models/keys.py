"""Claves API opacas de aplicaciones y personas (ADR-004 y ADR-006).

Solo se guarda el hash SHA-256 de la clave completa y un prefijo visible para
reconocerla en listados y logs. La generación y la verificación llegan en G3.
"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class KeyKind(StrEnum):
    APPLICATION = "application"
    USER = "user"


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        # Una clave pertenece exactamente a una aplicación o a una persona, según su tipo
        CheckConstraint(
            "(kind = 'application' AND application_id IS NOT NULL AND user_id IS NULL)"
            " OR (kind = 'user' AND user_id IS NOT NULL AND application_id IS NULL)",
            name="owner_matches_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"),
                                                 nullable=False, index=True)
    kind: Mapped[KeyKind] = mapped_column(
        Enum(KeyKind, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16), nullable=False)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="RESTRICT"),
                                                       nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True,
                                                index=True)
    # Parte visible, p. ej. "plgw-app-platia-3f9a": identifica la clave sin exponerla
    prefix: Mapped[str] = mapped_column(String(48), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # Rotación: la clave secundaria lleva caducidad y convive con la primaria
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
