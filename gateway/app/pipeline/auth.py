"""Etapa `auth`: de la clave a la aplicación, su equipo y su organización (cap. 2 y 18, ADR-004).

Una sola consulta exige a la vez: clave conocida, sin revocar ni caducar; aplicación
activa; equipo y organización activos. Si algo falla, 401 con el mismo mensaje (no se dice
qué parte falló). La persona final y el propósito se leen de las cabeceras `X-Platia-*`.

Una clave personal (`plgw-usr-`, G10, ADR-014) exige además la persona activa, y sigue las
reglas de la aplicación de la organización marcada para claves personales. La persona de
la clave es la persona final: no puede declarar otra con `X-Platia-User-Id`.
"""
import re
from datetime import timedelta

import structlog
from sqlalchemy import or_, select, update

from ..errors import AuthenticationFailed, InvalidRequest
from ..keys import hash_key, kind_of
from ..errors import PermissionDenied
from ..models import ApiKey, Application, ApplicationStatus, KeyKind, Organization, Team, User
from ..models.base import utcnow
from .context import Deps, Identity, PipelineContext

log = structlog.get_logger(__name__)

PURPOSE_HEADER = "x-platia-purpose"
END_USER_HEADER = "x-platia-user-id"
_PURPOSE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_END_USER = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
# last_used_at se actualiza como mucho cada 5 minutos: no una escritura por petición
_LAST_USED_EVERY = timedelta(minutes=5)
INVALID_KEY = "Clave API no válida, revocada o caducada"


def extract_key(headers) -> str | None:
    """La clave llega en `Authorization: Bearer` (SDK de OpenAI) o en `x-api-key` (SDK de Anthropic)."""
    authorization = headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
    header_key = headers.get("x-api-key", "").strip()
    if bearer and header_key and bearer != header_key:
        raise AuthenticationFailed("Se enviaron dos claves API distintas")
    return bearer or header_key or None


def personal_ref(user_id: int) -> str:
    """Persona final de una clave personal en cuotas y auditoría."""
    return f"usr:{user_id}"


async def _touch(db, key: ApiKey, now) -> None:
    if key.last_used_at is None or key.last_used_at < now - _LAST_USED_EVERY:
        await db.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
        await db.commit()


def _identity(key: ApiKey, app: Application, org: Organization, **extra) -> Identity:
    return Identity(
        organization_id=org.id, team_id=extra.pop("team_id", app.team_id), application_id=app.id,
        application_slug=app.slug, api_key_id=key.id, api_key_prefix=key.prefix,
        trusted=extra.pop("trusted", app.trusted), allowed_purposes=tuple(app.allowed_purposes or ()),
        allowed_models=tuple(app.allowed_models) if app.allowed_models is not None else None,
        allowed_providers=tuple(org.allowed_providers) if org.allowed_providers is not None else None,
        audit_salt=org.audit_salt, **extra,
    )


async def _resolve_personal(deps: Deps, raw: str) -> Identity:
    now = utcnow()
    query = (
        select(ApiKey, User, Organization)
        .join(User, ApiKey.user_id == User.id)
        .join(Organization, User.organization_id == Organization.id)
        .where(ApiKey.key_hash == hash_key(raw), ApiKey.kind == KeyKind.USER, ApiKey.revoked_at.is_(None),
               or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
               ApiKey.organization_id == User.organization_id,
               User.is_active.is_(True), User.deleted_at.is_(None),
               Organization.is_active.is_(True), Organization.deleted_at.is_(None))
    )
    async with deps.sessions() as db:
        row = (await db.execute(query)).first()
        if row is None:
            log.info("auth.rejected", reason="clave_personal_no_valida")
            raise AuthenticationFailed(INVALID_KEY)
        key, user, org = row
        apps = (await db.execute(
            select(Application).join(Team, Application.team_id == Team.id)
            .where(Application.organization_id == org.id, Application.personal_keys.is_(True),
                   Application.status == ApplicationStatus.ACTIVE, Application.deleted_at.is_(None),
                   Team.is_active.is_(True), Team.deleted_at.is_(None)).limit(2))).scalars().all()
        if len(apps) != 1:
            log.warning("auth.personal_app_missing", organization_id=org.id, found=len(apps))
            raise PermissionDenied("La organización no tiene activadas las claves personales")
        await _touch(db, key, now)
    # Equipo de la persona si lo tiene (cuotas por equipo); si no, el de la aplicación
    return _identity(key, apps[0], org, team_id=user.team_id or apps[0].team_id, trusted=False, user_id=user.id)


async def resolve(deps: Deps, raw: str) -> Identity:
    kind = kind_of(raw)
    if kind == KeyKind.USER:
        return await _resolve_personal(deps, raw)
    if kind != KeyKind.APPLICATION:
        raise AuthenticationFailed(INVALID_KEY)
    now = utcnow()
    query = (
        select(ApiKey, Application, Organization)
        .join(Application, ApiKey.application_id == Application.id)
        .join(Organization, Application.organization_id == Organization.id)
        .join(Team, Application.team_id == Team.id)
        .where(ApiKey.key_hash == hash_key(raw), ApiKey.kind == KeyKind.APPLICATION, ApiKey.revoked_at.is_(None),
               or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
               ApiKey.organization_id == Application.organization_id,
               Application.status == ApplicationStatus.ACTIVE, Application.deleted_at.is_(None),
               Organization.is_active.is_(True), Organization.deleted_at.is_(None),
               Team.is_active.is_(True), Team.deleted_at.is_(None))
    )
    async with deps.sessions() as db:
        row = (await db.execute(query)).first()
        if row is None:
            log.info("auth.rejected", reason="clave_no_valida")
            raise AuthenticationFailed(INVALID_KEY)
        key, app, org = row
        await _touch(db, key, now)
    return _identity(key, app, org)


async def run(ctx: PipelineContext, deps: Deps) -> None:
    raw = extract_key(ctx.headers)
    if not raw:
        raise AuthenticationFailed("Falta la clave API (cabecera Authorization: Bearer o x-api-key)")
    ctx.identity = await resolve(deps, raw)
    if ctx.identity.user_id is not None:
        ctx.end_user_ref = personal_ref(ctx.identity.user_id)

    purpose = ctx.headers.get(PURPOSE_HEADER)
    if purpose is not None:
        if not _PURPOSE.match(purpose):
            raise InvalidRequest("X-Platia-Purpose no válido: minúsculas, números, '_', '.' o '-' (máx. 64)")
        ctx.purpose = purpose

    end_user = ctx.headers.get(END_USER_HEADER)
    if end_user is not None:
        if not ctx.identity.trusted:
            raise InvalidRequest("Esta clave no puede declarar X-Platia-User-Id")
        if not _END_USER.match(end_user):
            raise InvalidRequest("X-Platia-User-Id no válido")
        ctx.end_user_ref = end_user
