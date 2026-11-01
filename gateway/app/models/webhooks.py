"""Webhooks firmados y sus entregas (G12, cap. 24, ADR-016).

Un webhook es un destino HTTPS que recibe ciertos eventos (avisos de cuota, bloqueos de
seguridad, despliegues enfriados) firmados con HMAC-SHA256. Puede ser de una organización
(recibe sus eventos) o global (`organization_id` nulo: recibe todos, para quien opera el
gateway). El secreto de firma se guarda cifrado con `app/crypto.py`.

Cada evento crea una entrega por webhook; un proceso en segundo plano las envía y reintenta.
Así un receptor caído no frena ninguna petición y nada se pierde si el gateway se reinicia.
Los modelos siguen machinebooks.ai/ES/aigateway/cap-24/block-13.py y block-15.py (MIT).
"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, utcnow


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"  # el webhook se desactivó o se borró antes de entregarla


class Webhook(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nulo: global, recibe los eventos de todas las organizaciones y los del propio gateway
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"),
                                                        nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Eventos que recibe, p. ej. ["quota.alert", "security.blocked"]
    events: Mapped[list] = mapped_column(JSON, nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Entregas fallidas seguidas (tras agotar sus reintentos): con demasiadas se desactiva solo
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    disabled_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


def _enum(cls):
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    webhook_id: Mapped[int] = mapped_column(ForeignKey("webhooks.id", ondelete="RESTRICT"), nullable=False,
                                            index=True)
    # El id del evento es el mismo en todos sus webhooks; el de la entrega, propio
    event_id: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    # Cuerpo exacto que se firma y se envía (sin texto de prompts ni respuestas)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(_enum(DeliveryStatus), default=DeliveryStatus.PENDING,
                                                   nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # Resultado del último intento: código HTTP, error breve (timeout, conexión, url_bloqueada) y duración
    last_status_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
