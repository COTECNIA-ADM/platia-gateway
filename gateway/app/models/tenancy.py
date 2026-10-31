"""Identidad: organización → equipo → persona, y aplicaciones colgando de un equipo (ADR-004)."""
import secrets
from enum import StrEnum

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin


def _new_salt() -> str:
    return secrets.token_hex(32)


class Organization(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Lista blanca de proveedores (slugs). NULL = todos los activos.
    allowed_providers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Sal para el hash de los prompts en la auditoría: el mismo texto da hashes
    # distintos en cada organización (cap. 21 y 32). Nunca sale del gateway.
    audit_salt: Mapped[str] = mapped_column(String(64), default=_new_salt, nullable=False)


class Team(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_teams_org_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"),
                                                 nullable=False, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UserRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class User(TimestampMixin, SoftDeleteMixin, Base):
    """Persona de la organización. Contraseña, MFA y sesiones llegan en G10."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_users_org_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"),
                                                 nullable=False, index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True,
                                                index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, values_callable=lambda e: [m.value for m in e],
                                                native_enum=False, length=16),
                                           default=UserRole.MEMBER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ApplicationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"  # pausa reversible
    REVOKED = "revoked"  # definitiva


class Application(TimestampMixin, SoftDeleteMixin, Base):
    """Cliente registrado (por ejemplo, PLATIA). Pertenece a un equipo y tiene sus propias reglas."""

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_applications_org_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"),
                                                 nullable=False, index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16),
        default=ApplicationStatus.ACTIVE, nullable=False)
    # De confianza: puede declarar la persona final con X-Platia-User-Id (ADR-004)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Propósitos que puede declarar con X-Platia-Purpose. Lista vacía = solo "default";
    # ["*"] = cualquiera (aplicaciones que gobiernan sus propias tareas, como PLATIA).
    allowed_purposes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Lista cerrada de modelos (model_key). NULL = los que permita la organización.
    allowed_models: Mapped[list | None] = mapped_column(JSON, nullable=True)
