"""Catálogo de LLM: proveedores, modelos, despliegues y precios (cap. 5, 6, 7 y 14).

- **Proveedor**: una cuenta en Anthropic o en una API compatible con OpenAI. Su clave
  se guarda cifrada (`app.crypto`).
- **Modelo**: el nombre que piden los clientes (`claude-sonnet-5-5`) y lo que sabe hacer.
- **Despliegue**: un modelo servido por un proveedor concreto, con el nombre que ese
  proveedor usa; un modelo puede tener varios (reparto por peso y reserva, G6).
- **Precio**: USD por millón de tokens, con fecha de inicio; nunca se sobrescribe.
"""
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (JSON, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, utcnow


def _enum(cls, length: int = 32):
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=length)


class ProviderKind(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelFamily(StrEnum):
    """Familia del contrato nativo del modelo (ADR-002: paso directo entre iguales)."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Provider(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[ProviderKind] = mapped_column(_enum(ProviderKind), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # "enc:v1:..." (AES-256-GCM). Nunca se devuelve en una respuesta ni va a un log.
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LlmModel(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "llm_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    family: Mapped[ModelFamily] = mapped_column(_enum(ModelFamily), nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Funciones que admite: "tools", "thinking", "structured_output", "vision", "prompt_cache"...
    # Un destino sin una función que la petición usa no es elegible (ADR-002).
    capabilities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Deployment(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("model_id", "provider_id", "upstream_model", name="uq_deployments_model_provider_upstream"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("llm_models.id", ondelete="RESTRICT"), nullable=False,
                                          index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False,
                                             index=True)
    # Nombre del modelo en el proveedor (en Azure, el nombre del despliegue)
    upstream_model: Mapped[str] = mapped_column(String(255), nullable=False)
    # Reparto entre despliegues del mismo modelo (G6): mayor peso, más tráfico
    weight: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    # Orden en la cadena de reserva: 0 primero
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ModelPrice(Base):
    """Precio de un modelo desde una fecha. Un cambio de precio es una fila nueva."""

    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("model_id", "effective_from", name="uq_model_prices_model_from"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("llm_models.id", ondelete="RESTRICT"), nullable=False,
                                          index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # USD por millón de tokens
    input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    output_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    # NULL = sin precio propio: se cobra como entrada normal
    cache_read_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cache_write_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
