"""Límites de uso: cuotas de gasto, peticiones y tokens por periodo, y peticiones simultáneas (G7, ADR-011).

Un límite se aplica a un ámbito: la organización, un equipo, una aplicación o las personas
de una aplicación (`end_user`). En `end_user`, `subject` vacío vale para cada persona por
separado y un `subject` concreto, solo para esa persona (y sustituye al general).

Las filas son configuración (borrado lógico); los contadores viven en Redis y se
reconstruyen desde la auditoría si se pierden (`app.limits`).
"""
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, Enum, Index, Integer, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin


class LimitScope(StrEnum):
    ORGANIZATION = "organization"
    TEAM = "team"
    APPLICATION = "application"
    END_USER = "end_user"


class LimitMetric(StrEnum):
    COST_USD = "cost_usd"
    REQUESTS = "requests"
    TOKENS = "tokens"
    CONCURRENT = "concurrent"


class LimitPeriod(StrEnum):
    MINUTE = "minute"
    DAY = "day"
    MONTH = "month"
    # Peticiones simultáneas: no tienen periodo
    NONE = "none"


def _enum(cls):
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16)


class UsageLimit(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "usage_limits"
    __table_args__ = (Index("ix_usage_limits_scope", "scope", "scope_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[LimitScope] = mapped_column(_enum(LimitScope), nullable=False)
    # Id de la organización, el equipo o la aplicación (en end_user, el de la aplicación)
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Solo en end_user: la persona concreta (X-Platia-User-Id); vacío = cada persona
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metric: Mapped[LimitMetric] = mapped_column(_enum(LimitMetric), nullable=False)
    period: Mapped[LimitPeriod] = mapped_column(_enum(LimitPeriod), nullable=False)
    # USD en cost_usd; número de peticiones o de tokens en los demás
    limit_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    # Aviso en el log al pasar este porcentaje (una vez por periodo)
    alert_pct: Mapped[int] = mapped_column(SmallInteger, default=80, server_default="80", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
