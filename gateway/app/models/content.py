"""Política de contenido: qué hacer con cada tipo de hallazgo en la entrada y en la salida (G8, ADR-012).

Sin filas, vale la política por defecto (`app.pipeline.security_input.DEFAULTS`). Una fila
de la organización cambia una casilla para todas sus aplicaciones; una de la aplicación
manda sobre la de su organización.
"""
from enum import StrEnum

from sqlalchemy import Enum, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin


class PolicyScope(StrEnum):
    ORGANIZATION = "organization"
    APPLICATION = "application"


class Direction(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class ContentCheck(StrEnum):
    JAILBREAK = "jailbreak"
    CLASSIFIED = "classified"
    SECRET = "secret"
    PII = "pii"


class ContentAction(StrEnum):
    BLOCK = "block"  # se rechaza (entrada) o se corta (salida)
    REDACT = "redact"  # se tacha lo encontrado y la petición sigue
    LOG = "log"  # solo se anota en la auditoría y en el log
    OFF = "off"  # no se comprueba


def _enum(cls):
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16)


class ContentPolicy(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "content_policies"
    __table_args__ = (UniqueConstraint("scope", "scope_id", "direction", "check_name",
                                       name="uq_content_policies_rule"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[PolicyScope] = mapped_column(_enum(PolicyScope), nullable=False)
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[Direction] = mapped_column(_enum(Direction), nullable=False)
    check_name: Mapped[ContentCheck] = mapped_column(_enum(ContentCheck), nullable=False)
    action: Mapped[ContentAction] = mapped_column(_enum(ContentAction), nullable=False)
