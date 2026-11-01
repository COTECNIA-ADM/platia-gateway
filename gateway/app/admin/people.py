"""Personas de la organización desde el panel: altas, roles, contraseñas, MFA y sesiones (G13).

Un administrador no puede quitarse a sí mismo el rol ni darse de baja, y la organización
nunca se queda sin administradores activos: así nadie se cierra la puerta por error. Ser
operador del gateway no se da desde el panel, solo con la CLI.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import func, select, update

from .. import passwords
from ..errors import InvalidRequest
from ..identity import Principal
from ..models import ApiKey, KeyKind, Team, User, UserRole, UserSession
from ..models.base import utcnow
from ..pipeline.runner import deps_of
from .common import Body, admin, commit, flush, get_owned, iso, record

router = APIRouter(tags=["panel: personas"])
EMAIL = r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$"


class UserIn(Body):
    email: str = Field(pattern=EMAIL, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    role: UserRole = UserRole.MEMBER
    team_id: int | None = None
    password: str | None = Field(default=None, max_length=256)


class UserPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: UserRole | None = None
    team_id: int | None = None
    is_active: bool | None = None


class PasswordIn(Body):
    password: str = Field(min_length=1, max_length=256)


class RevokeIn(Body):
    keys: bool = False


def _user_body(user: User, sessions: int = 0, keys: int = 0) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": str(user.role), "team_id": user.team_id,
            "is_active": user.is_active, "mfa_enabled": user.mfa_enabled, "has_password": bool(user.password_hash),
            "platform_operator": user.platform_operator, "last_login_at": iso(user.last_login_at),
            "active_sessions": sessions, "active_keys": keys, "created_at": iso(user.created_at)}


async def _counts(db, user_ids: list[int]) -> tuple[dict, dict]:
    now = utcnow()
    sessions = dict((await db.execute(select(UserSession.user_id, func.count()).where(
        UserSession.user_id.in_(user_ids), UserSession.revoked_at.is_(None), UserSession.expires_at > now)
        .group_by(UserSession.user_id))).all())
    keys = dict((await db.execute(select(ApiKey.user_id, func.count()).where(
        ApiKey.user_id.in_(user_ids), ApiKey.kind == KeyKind.USER, ApiKey.revoked_at.is_(None),
        (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now)).group_by(ApiKey.user_id))).all())
    return sessions, keys


async def _body(db, user: User) -> dict:
    sessions, keys = await _counts(db, [user.id])
    return _user_body(user, sessions.get(user.id, 0), keys.get(user.id, 0))


def _password(value: str, email: str) -> str:
    try:
        passwords.check_policy(value, email=email)
    except passwords.WeakPassword as exc:
        raise InvalidRequest(str(exc)) from exc
    return passwords.hash_password(value)


async def _close_sessions(db, user_id: int, reason: str) -> int:
    result = await db.execute(update(UserSession).where(UserSession.user_id == user_id,
                                                        UserSession.revoked_at.is_(None))
                              .values(revoked_at=utcnow(), revoked_reason=reason))
    return result.rowcount or 0


async def _revoke_personal_keys(db, user_id: int) -> int:
    result = await db.execute(update(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
                              .values(revoked_at=utcnow()))
    return result.rowcount or 0


async def _other_admins(db, organization_id: int, user_id: int) -> int:
    return (await db.execute(select(func.count()).select_from(User).where(
        User.organization_id == organization_id, User.id != user_id, User.role == UserRole.ADMIN,
        User.is_active.is_(True), User.deleted_at.is_(None)))).scalar()


@router.get("/users")
async def list_users(request: Request, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        users = (await db.execute(select(User).where(User.organization_id == who.organization_id,
                                                     User.deleted_at.is_(None)).order_by(User.email))).scalars().all()
        sessions, keys = await _counts(db, [u.id for u in users]) if users else ({}, {})
        return {"data": [_user_body(u, sessions.get(u.id, 0), keys.get(u.id, 0)) for u in users]}


@router.post("/users", status_code=201)
async def create_user(request: Request, body: UserIn, who: Principal = Depends(admin)):
    email = body.email.strip().lower()
    async with deps_of(request).sessions() as db:
        if body.team_id is not None:
            await get_owned(db, Team, body.team_id, who.organization_id, "el equipo")
        user = User(organization_id=who.organization_id, email=email, name=body.name, role=body.role,
                    team_id=body.team_id)
        if body.password:
            user.password_hash, user.password_changed_at = _password(body.password, email), utcnow()
        db.add(user)
        await flush(db, f"Ya existe una persona con el correo {email}")
        record(db, request, who, "user.create", "user", user.id, body.model_dump(exclude={"password"}) | (
            {"password": True} if body.password else {}))
        await commit(db, f"Ya existe una persona con el correo {email}")
        return await _body(db, user)


@router.patch("/users/{user_id}")
async def update_user(request: Request, user_id: int, body: UserPatch, who: Principal = Depends(admin)):
    changes = body.model_dump(exclude_unset=True)
    for field in ("name", "role", "is_active"):
        if field in changes and changes[field] is None:
            raise InvalidRequest(f"'{field}' no puede ser nulo")
    async with deps_of(request).sessions() as db:
        user = await get_owned(db, User, user_id, who.organization_id, "la persona")
        demoted = changes.get("role", user.role) != UserRole.ADMIN or changes.get("is_active", user.is_active) is False
        if user.id == who.user_id and demoted:
            raise InvalidRequest("No puedes quitarte el rol de administrador ni darte de baja a ti")
        if user.role == UserRole.ADMIN and user.is_active and demoted \
                and not await _other_admins(db, who.organization_id, user.id):
            raise InvalidRequest("La organización se quedaría sin administradores activos")
        if changes.get("team_id") is not None:
            await get_owned(db, Team, changes["team_id"], who.organization_id, "el equipo")
        for field, value in changes.items():
            setattr(user, field, value)
        if changes.get("is_active") is False or ("role" in changes and changes["role"] != UserRole.ADMIN):
            # Un cambio de permisos no espera a que caduque la sesión
            await _close_sessions(db, user.id, "admin")
        record(db, request, who, "user.update", "user", user.id, changes)
        await commit(db)
        return await _body(db, user)


@router.post("/users/{user_id}/password")
async def set_password(request: Request, user_id: int, body: PasswordIn, who: Principal = Depends(admin)):
    """Contraseña inicial o restablecida (la persona debería cambiarla al entrar). Cierra sus sesiones."""
    async with deps_of(request).sessions() as db:
        user = await get_owned(db, User, user_id, who.organization_id, "la persona")
        if user.id == who.user_id:
            raise InvalidRequest("Tu propia contraseña se cambia en tu cuenta, con la actual")
        user.password_hash, user.password_changed_at = _password(body.password, user.email), utcnow()
        closed = await _close_sessions(db, user.id, "password_changed")
        record(db, request, who, "user.set_password", "user", user.id, {"password": True})
        await commit(db)
        return {"sessions_closed": closed}


@router.post("/users/{user_id}/reset-mfa")
async def reset_mfa(request: Request, user_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        user = await get_owned(db, User, user_id, who.organization_id, "la persona")
        if user.id == who.user_id:
            raise InvalidRequest("Tu propio MFA se quita en tu cuenta")
        user.mfa_enabled, user.mfa_secret_encrypted, user.mfa_last_step = False, None, None
        closed = await _close_sessions(db, user.id, "mfa_changed")
        record(db, request, who, "user.reset_mfa", "user", user.id)
        await commit(db)
        return {"sessions_closed": closed}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_sessions(request: Request, user_id: int, body: RevokeIn, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        user = await get_owned(db, User, user_id, who.organization_id, "la persona")
        closed = await _close_sessions(db, user.id, "admin")
        keys = await _revoke_personal_keys(db, user.id) if body.keys else 0
        record(db, request, who, "user.revoke_sessions", "user", user.id, {"keys": body.keys})
        await commit(db)
        return {"sessions_closed": closed, "keys_revoked": keys}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(request: Request, user_id: int, who: Principal = Depends(admin)):
    """Baja: borrado lógico, sesiones cerradas y claves personales revocadas. La auditoría se conserva."""
    async with deps_of(request).sessions() as db:
        user = await get_owned(db, User, user_id, who.organization_id, "la persona")
        if user.id == who.user_id:
            raise InvalidRequest("No puedes darte de baja a ti")
        if user.role == UserRole.ADMIN and user.is_active and not await _other_admins(db, who.organization_id,
                                                                                      user.id):
            raise InvalidRequest("La organización se quedaría sin administradores activos")
        user.deleted_at, user.is_active = utcnow(), False
        await _close_sessions(db, user.id, "admin")
        await _revoke_personal_keys(db, user.id)
        record(db, request, who, "user.delete", "user", user.id, {"email": user.email})
        await commit(db)
    return Response(status_code=204)
