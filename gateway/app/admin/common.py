"""Lo común de la API del panel: quién puede entrar, el registro de cambios y utilidades (G13, ADR-017).

- **Administrador** de una organización (rol `admin`, sesión completa con MFA): todo lo de
  su organización. Nunca ve ni cambia otra.
- **Operador** del gateway (`users.platform_operator`, solo desde la CLI): además, el
  catálogo común (proveedores, modelos, despliegues, precios) y los webhooks globales. Los
  administradores ven el catálogo, pero no lo cambian.

Cada cambio deja una fila en `admin_actions` con quién, qué y los valores nuevos, en la
misma transacción que el cambio: si no se puede registrar, no se hace. Los secretos nunca
entran en el registro.
"""
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import identity
from ..errors import Conflict, NotFound, PermissionDenied
from ..identity import Principal
from ..models import AdminAction, Organization, UserRole
from ..pipeline.runner import deps_of, request_id_of

# Campos que nunca se guardan en el registro de cambios, aunque lleguen en el cuerpo
SECRET_FIELDS = frozenset({"api_key", "password", "secret", "key"})


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value[:7].lower() == "bearer " else None


async def admin(request: Request) -> Principal:
    """Administrador de su organización, con la sesión completa (MFA hecho si se exige)."""
    who = await identity.authenticate(deps_of(request), _bearer(request))
    if who.role != UserRole.ADMIN:
        raise PermissionDenied("Solo para administradores de la organización")
    return who


async def operator(request: Request) -> Principal:
    who = await admin(request)
    if not who.platform_operator:
        raise PermissionDenied("Solo quien opera el gateway puede cambiar el catálogo común")
    return who


def iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if value else None


def plain(value):
    """Un valor de la base en JSON: fechas ISO, decimales como texto, enumeraciones por su valor."""
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    return value


def ip_of(request: Request) -> str | None:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


def record(db: AsyncSession, request: Request, who: Principal, action: str, target_type: str,
           target_id: object | None, changes: dict | None = None, *, organization_id: int | None | bool = True) -> None:
    """Añade la fila del registro a la transacción en curso. `organization_id=True`: la de quien cambia."""
    safe = {k: ("(cambiado)" if k in SECRET_FIELDS else plain(v)) for k, v in (changes or {}).items()}
    db.add(AdminAction(organization_id=who.organization_id if organization_id is True else organization_id,
                       user_id=who.user_id, action=action, target_type=target_type,
                       target_id=None if target_id is None else str(target_id), changes=safe or None,
                       ip_address=ip_of(request), request_id=request_id_of(request)))


async def commit(db: AsyncSession, what: str = "Ya existe un registro con esos datos") -> None:
    """Confirma la transacción; un duplicado (slug, correo, prefijo) es un 409, no un 500."""
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise Conflict(what) from exc


async def flush(db: AsyncSession, what: str = "Ya existe un registro con esos datos") -> None:
    """Como `commit`, para cuando hace falta el id antes de confirmar."""
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise Conflict(what) from exc


async def get_owned(db: AsyncSession, model, object_id: int, organization_id: int, what: str):
    """Un objeto de la organización (y no borrado); si es de otra, como si no existiera."""
    row = await db.get(model, object_id)
    if (row is None or getattr(row, "organization_id", organization_id) != organization_id
            or getattr(row, "deleted_at", None) is not None):
        raise NotFound(f"No existe {what} {object_id}")
    return row


async def get_live(db: AsyncSession, model, object_id: int, what: str):
    row = await db.get(model, object_id)
    if row is None or getattr(row, "deleted_at", None) is not None:
        raise NotFound(f"No existe {what} {object_id}")
    return row


async def organization(db: AsyncSession, organization_id: int) -> Organization:
    return (await db.execute(select(Organization).where(Organization.id == organization_id))).scalar_one()


def page(items: list, limit: int) -> dict:
    """Lista con cursor: `next` es el id desde el que pedir la página siguiente."""
    more = len(items) > limit
    items = items[:limit]
    return {"data": items, "next": items[-1]["id"] if more and items else None}
