"""Auditoría de cada petición (cap. 21 y 32).

Guarda metadatos, nunca el texto: el prompt queda como hash con la sal de la
organización. Los ids de organización, aplicación, etc. son enteros sin clave
foránea, para que la auditoría sobreviva a cambios en la configuración.

La escritura en dos pasos, la cadena de hashes y el trigger que impide modificar
filas llegan en G9 (ADR-005).
"""
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, Enum, Index, Integer, Numeric, SmallInteger, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow

# DATETIME(6): microsegundos, para ordenar peticiones del mismo segundo
_PRECISE = DATETIME(fsp=6)


class AuditStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"


class Contract(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


def _enum(cls):
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        Index("ix_audit_logs_app_created", "application_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(_PRECISE, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(_PRECISE, nullable=True)

    # Quién
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Persona final declarada por una aplicación de confianza (X-Platia-User-Id)
    end_user_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_prefix: Mapped[str | None] = mapped_column(String(48), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Qué
    contract: Mapped[Contract] = mapped_column(_enum(Contract), nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Resultado
    status: Mapped[AuditStatus] = mapped_column(_enum(AuditStatus), default=AuditStatus.PENDING, nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=Decimal("0"), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Huella del contenido, sin el contenido
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash_version: Mapped[str] = mapped_column(String(4), default="v1", nullable=False)
    # Decisiones de seguridad: [{"check": "pii", "action": "redact", "count": 2}, ...]
    security_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Cadena de integridad (G9)
    chain_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
