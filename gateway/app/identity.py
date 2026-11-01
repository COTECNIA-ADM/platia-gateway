"""Identidad de personas: inicio de sesión, MFA, sesiones revocables y claves personales
(G10, cap. 16 a 18, ADR-014).

- **Acceso**: JWT HS256 de vida corta (`ACCESS_TOKEN_MINUTES`) que lleva la persona, su
  organización, la sesión y el alcance. En cada petición se comprueba además en MySQL que
  la sesión sigue viva y la persona y la organización activas: revocar surte efecto al
  momento, sin lista negra en Redis.
- **Renovación**: opaca (``<sesión>.<secreto>``) en una cookie HttpOnly; cada uso la
  cambia, y si llega una ya usada la sesión se revoca entera (alguien la copió).
- **MFA**: TOTP. Con la contraseña correcta y MFA activado se entrega un vale de 5 minutos
  que solo sirve para enviar el código (5 intentos, un solo uso).
- **Alcance**: un administrador sin MFA (con `REQUIRE_MFA_FOR_ADMINS`) solo puede
  configurarlo hasta activarlo.
- **Bloqueo**: tras `LOGIN_MAX_FAILURES` contraseñas fallidas por persona y dirección,
  `LOGIN_LOCKOUT_SECONDS` de espera. Sin Redis no se puede contar: se rechaza (503).

Basado en machinebooks.ai/ES/aigateway/cap-16/block-02.py, block-03.py, block-12.py y
block-16.py, y cap-17/block-13.py (MIT); las sesiones en MySQL sustituyen a la lista negra.
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
import structlog
from sqlalchemy import func, select, update

from . import passwords, totp
from .crypto import CryptoError, decrypt, encrypt
from .errors import AuthenticationFailed, InvalidRequest, PermissionDenied, RateLimited, Unavailable
from .keys import new_user_key
from .models import (ApiKey, Application, ApplicationStatus, KeyKind, Organization, User, UserRole,
                     UserSession)
from .models.base import utcnow
from .pipeline.context import Deps

log = structlog.get_logger(__name__)

ALGORITHM = "HS256"  # fija: nunca se lee del token ni de la configuración (cap. 16)
MFA_TICKET_SECONDS = 300
MFA_MAX_ATTEMPTS = 5
SCOPE_FULL, SCOPE_SETUP = "full", "mfa_setup"
PERSONAL_KEYS_PER_USER = 5
PERSONAL_KEY_MAX_DAYS = 365
BAD_CREDENTIALS = "Correo o contraseña incorrectos"
SESSION_EXPIRED = "La sesión no es válida o ha caducado: vuelve a iniciar sesión"


@dataclass(frozen=True)
class Principal:
    """Persona autenticada en una petición del panel o del portal."""
    user_id: int
    organization_id: int
    session_id: int
    scope: str
    role: UserRole
    email: str
    name: str
    mfa_enabled: bool


@dataclass(frozen=True)
class Tokens:
    access_token: str
    expires_in: int
    refresh_token: str
    scope: str


@dataclass(frozen=True)
class LoginResult:
    tokens: Tokens | None = None
    mfa_ticket: str | None = None


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret(deps: Deps) -> str:
    if not deps.settings.jwt_secret:
        raise Unavailable("El inicio de sesión no está configurado (JWT_SECRET)")
    return deps.settings.jwt_secret


def _encryption_key(deps: Deps) -> bytes:
    if deps.encryption_key is None:
        raise Unavailable("El MFA necesita ENCRYPTION_KEY")
    return deps.encryption_key


def _mfa_context(user_id: int) -> str:
    return f"user-mfa:{user_id}"


def _scope_for(deps: Deps, user: User) -> str:
    if deps.settings.require_mfa_for_admins and user.role == UserRole.ADMIN and not user.mfa_enabled:
        return SCOPE_SETUP
    return SCOPE_FULL


# --- Tokens

def _encode(deps: Deps, claims: dict, seconds: int) -> str:
    now = utcnow()
    payload = {**claims, "iat": now, "exp": now + timedelta(seconds=seconds), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, _secret(deps), algorithm=ALGORITHM)


def _decode(deps: Deps, token: str, kind: str) -> dict:
    try:
        claims = jwt.decode(token, _secret(deps), algorithms=[ALGORITHM],
                            options={"require": ["exp", "iat", "sub", "typ", "jti"]})
    except jwt.PyJWTError as exc:
        raise AuthenticationFailed(SESSION_EXPIRED) from exc
    if claims.get("typ") != kind:
        raise AuthenticationFailed(SESSION_EXPIRED)
    return claims


def _access_token(deps: Deps, user: User, session_id: int) -> tuple[str, int, str]:
    seconds = deps.settings.access_token_minutes * 60
    scope = _scope_for(deps, user)
    token = _encode(deps, {"typ": "access", "sub": str(user.id), "org": user.organization_id, "sid": session_id,
                           "scope": scope}, seconds)
    return token, seconds, scope


# --- Bloqueo por intentos fallidos

def _failure_key(email: str, ip: str) -> str:
    return "gw:login:fail:" + _sha(f"{email}|{ip}")[:32]


async def _locked_out(deps: Deps, key: str) -> None:
    if deps.redis is None:
        raise Unavailable("El inicio de sesión no está disponible ahora mismo")
    try:
        failures = int(await deps.redis.get(key) or 0)
        ttl = await deps.redis.ttl(key) if failures >= deps.settings.login_max_failures else 0
    except Exception as exc:  # noqa: BLE001 - sin Redis no se cuentan los intentos: se rechaza
        log.error("auth.redis_failed", error_type=type(exc).__name__)
        raise Unavailable("El inicio de sesión no está disponible ahora mismo") from exc
    if failures >= deps.settings.login_max_failures:
        raise RateLimited("Demasiados intentos fallidos. Espera unos minutos", retry_after=max(ttl, 1))


async def _record_failure(deps: Deps, key: str) -> None:
    try:
        pipe = deps.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, deps.settings.login_lockout_seconds)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        log.error("auth.redis_failed", error_type=type(exc).__name__)


# --- Sesiones

async def _new_session(deps: Deps, db, user: User, ip: str | None, user_agent: str | None) -> Tokens:
    now = utcnow()
    secret = secrets.token_urlsafe(32)
    settings = deps.settings
    session = UserSession(user_id=user.id, organization_id=user.organization_id, refresh_hash=_sha(secret),
                          created_at=now, last_used_at=now,
                          expires_at=now + timedelta(days=settings.session_idle_days),
                          max_expires_at=now + timedelta(days=settings.session_max_days),
                          ip_address=(ip or None) and ip[:45], user_agent=(user_agent or None) and user_agent[:255])
    db.add(session)
    user.last_login_at = now
    await db.flush()
    access, seconds, scope = _access_token(deps, user, session.id)
    log.info("auth.login", user_id=user.id, organization_id=user.organization_id, session_id=session.id, scope=scope)
    return Tokens(access, seconds, f"{session.id}.{secret}", scope)


async def _revoke_sessions(db, user_id: int, reason: str, *, keep: int | None = None) -> int:
    where = [UserSession.user_id == user_id, UserSession.revoked_at.is_(None)]
    if keep is not None:
        where.append(UserSession.id != keep)
    result = await db.execute(update(UserSession).where(*where).values(revoked_at=utcnow(), revoked_reason=reason))
    return result.rowcount


def _active_user_query():
    return (select(User).join(Organization, User.organization_id == Organization.id)
            .where(User.is_active.is_(True), User.deleted_at.is_(None),
                   Organization.is_active.is_(True), Organization.deleted_at.is_(None)))


# --- Inicio de sesión

async def login(deps: Deps, email: str, password: str, organization: str | None,
                ip: str | None, user_agent: str | None) -> LoginResult:
    _secret(deps)
    email = email.strip().lower()
    failure_key = _failure_key(email, ip or "")
    await _locked_out(deps, failure_key)
    query = _active_user_query().where(func.lower(User.email) == email)
    if organization:
        query = query.where(Organization.slug == organization)
    async with deps.sessions() as db, db.begin():
        candidates = (await db.execute(query)).scalars().all()
        matches = [u for u in candidates if passwords.verify_password(password, u.password_hash)]
        if not candidates:
            passwords.verify_password(password, passwords.DUMMY_HASH)  # mismo tiempo que con una persona real
        if not matches:
            await _record_failure(deps, failure_key)
            log.info("auth.login_failed", reason="credenciales")
            raise AuthenticationFailed(BAD_CREDENTIALS)
        if len(matches) > 1:
            raise InvalidRequest("Esta cuenta existe en varias organizaciones: indica cuál")
        user = matches[0]
        try:
            await deps.redis.delete(failure_key)
        except Exception:  # noqa: BLE001 - el contador caduca solo
            log.warning("auth.redis_failed", error_type="delete")
        if passwords.needs_rehash(user.password_hash):
            user.password_hash = passwords.hash_password(password)
        if user.mfa_enabled:
            ticket = _encode(deps, {"typ": "mfa", "sub": str(user.id)}, MFA_TICKET_SECONDS)
            log.info("auth.mfa_required", user_id=user.id)
            return LoginResult(mfa_ticket=ticket)
        return LoginResult(tokens=await _new_session(deps, db, user, ip, user_agent))


async def _check_mfa_code(deps: Deps, user: User, code: str, error=AuthenticationFailed) -> None:
    """Comprueba un código TOTP. Al iniciar sesión un fallo es 401; con la sesión ya
    abierta (activar o desactivar el MFA) es 400, para que el panel no la dé por cerrada."""
    if not user.mfa_secret_encrypted:
        raise InvalidRequest("Primero hay que configurar el MFA")
    try:
        secret = decrypt(user.mfa_secret_encrypted, key=_encryption_key(deps), context=_mfa_context(user.id))
    except CryptoError as exc:
        log.error("auth.mfa_secret_unreadable", user_id=user.id)
        raise Unavailable("No se puede comprobar el código ahora mismo") from exc
    step = totp.verify(secret, code, after_step=user.mfa_last_step)
    if step is None:
        raise error("Código incorrecto o ya usado")
    user.mfa_last_step = step


async def complete_mfa(deps: Deps, ticket: str, code: str, ip: str | None, user_agent: str | None) -> Tokens:
    claims = _decode(deps, ticket, "mfa")
    tries_key, used_key = f"gw:mfa:tries:{claims['jti']}", f"gw:mfa:used:{claims['jti']}"
    if deps.redis is None:
        raise Unavailable("El inicio de sesión no está disponible ahora mismo")
    try:
        tries = await deps.redis.incr(tries_key)
        await deps.redis.expire(tries_key, MFA_TICKET_SECONDS)
        used = await deps.redis.exists(used_key)
    except Exception as exc:  # noqa: BLE001 - sin Redis no se cuentan los intentos: se rechaza
        log.error("auth.redis_failed", error_type=type(exc).__name__)
        raise Unavailable("El inicio de sesión no está disponible ahora mismo") from exc
    if used or tries > MFA_MAX_ATTEMPTS:
        raise AuthenticationFailed("Demasiados intentos o vale ya usado: vuelve a iniciar sesión")
    async with deps.sessions() as db, db.begin():
        user = (await db.execute(_active_user_query().where(User.id == int(claims["sub"]))
                                 .with_for_update())).scalar()
        if user is None or not user.mfa_enabled:
            raise AuthenticationFailed(SESSION_EXPIRED)
        try:
            await _check_mfa_code(deps, user, code)
        except AuthenticationFailed:
            log.info("auth.mfa_failed", user_id=user.id, attempt=tries)
            raise
        if not await deps.redis.set(used_key, "1", ex=MFA_TICKET_SECONDS, nx=True):
            raise AuthenticationFailed("Vale ya usado: vuelve a iniciar sesión")
        return await _new_session(deps, db, user, ip, user_agent)


def _parse_refresh(raw: str | None) -> tuple[int, str]:
    session_id, _, secret = (raw or "").partition(".")
    if not session_id.isdigit() or not secret:
        raise AuthenticationFailed(SESSION_EXPIRED)
    return int(session_id), secret


async def refresh(deps: Deps, raw: str | None) -> Tokens:
    _secret(deps)
    session_id, secret = _parse_refresh(raw)
    now = utcnow()
    async with deps.sessions() as db, db.begin():
        session = (await db.execute(select(UserSession).where(UserSession.id == session_id)
                                    .with_for_update())).scalar()
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            raise AuthenticationFailed(SESSION_EXPIRED)
        if not secrets.compare_digest(session.refresh_hash, _sha(secret)):
            # Una renovación ya usada: alguien tiene una copia. Se corta la sesión entera
            # (se confirma antes de responder: un error desharía la transacción)
            session.revoked_at, session.revoked_reason = now, "refresh_reused"
            await db.commit()
            log.warning("auth.refresh_reused", user_id=session.user_id, session_id=session.id)
            raise AuthenticationFailed(SESSION_EXPIRED)
        user = (await db.execute(_active_user_query().where(User.id == session.user_id))).scalar()
        if user is None:
            raise AuthenticationFailed(SESSION_EXPIRED)
        new_secret = secrets.token_urlsafe(32)
        session.refresh_hash = _sha(new_secret)
        session.last_used_at = now
        session.expires_at = min(now + timedelta(days=deps.settings.session_idle_days), session.max_expires_at)
        access, seconds, scope = _access_token(deps, user, session.id)
        return Tokens(access, seconds, f"{session.id}.{new_secret}", scope)


async def logout(deps: Deps, *, session_id: int | None = None, refresh_token: str | None = None) -> None:
    """Cierra la sesión del token de acceso o de la cookie de renovación (lo que llegue)."""
    if session_id is None and refresh_token:
        try:
            session_id, secret = _parse_refresh(refresh_token)
        except AuthenticationFailed:
            return
        async with deps.sessions() as db:
            row = await db.get(UserSession, session_id)
            if row is None or not secrets.compare_digest(row.refresh_hash, _sha(secret)):
                return
    if session_id is None:
        return
    async with deps.sessions() as db, db.begin():
        await db.execute(update(UserSession).where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
                         .values(revoked_at=utcnow(), revoked_reason="logout"))


async def authenticate(deps: Deps, token: str | None, *, allow_setup: bool = False) -> Principal:
    """Persona de una petición con `Authorization: Bearer <acceso>`. Sesión, persona y
    organización se comprueban en MySQL en cada petición: revocar surte efecto al momento."""
    if not token:
        raise AuthenticationFailed("Falta el token de acceso (Authorization: Bearer)")
    claims = _decode(deps, token, "access")
    now = utcnow()
    async with deps.sessions() as db:
        row = (await db.execute(
            _active_user_query().add_columns(UserSession)
            .join(UserSession, UserSession.user_id == User.id)
            .where(UserSession.id == claims.get("sid"), User.id == int(claims["sub"]),
                   UserSession.revoked_at.is_(None), UserSession.expires_at > now))).first()
    if row is None:
        raise AuthenticationFailed(SESSION_EXPIRED)
    user, session = row
    scope = claims.get("scope")
    if scope != SCOPE_FULL and not (allow_setup and scope == SCOPE_SETUP):
        raise PermissionDenied("Activa el MFA antes de continuar: es obligatorio para administradores")
    return Principal(user_id=user.id, organization_id=user.organization_id, session_id=session.id, scope=scope,
                     role=user.role, email=user.email, name=user.name, mfa_enabled=user.mfa_enabled)


# --- Cuenta

async def change_password(deps: Deps, who: Principal, current: str, new: str) -> int:
    async with deps.sessions() as db, db.begin():
        user = await db.get(User, who.user_id, with_for_update=True)
        if not passwords.verify_password(current, user.password_hash):
            raise PermissionDenied("La contraseña actual no es correcta")
        try:
            passwords.check_policy(new, email=user.email)
        except passwords.WeakPassword as exc:
            raise InvalidRequest(str(exc)) from exc
        user.password_hash, user.password_changed_at = passwords.hash_password(new), utcnow()
        closed = await _revoke_sessions(db, user.id, "password_changed", keep=who.session_id)
    log.info("auth.password_changed", user_id=who.user_id, other_sessions_closed=closed)
    return closed


async def mfa_setup(deps: Deps, who: Principal) -> tuple[str, str]:
    """Secreto nuevo (aún no activo) y su enlace otpauth:// para la app."""
    key = _encryption_key(deps)
    async with deps.sessions() as db, db.begin():
        user = await db.get(User, who.user_id, with_for_update=True)
        if user.mfa_enabled:
            raise InvalidRequest("El MFA ya está activado")
        secret = totp.new_secret()
        user.mfa_secret_encrypted = encrypt(secret, key=key, context=_mfa_context(user.id))
        user.mfa_last_step = None
    return secret, totp.provisioning_uri(secret, who.email)


async def mfa_enable(deps: Deps, who: Principal, code: str) -> tuple[str, int, str]:
    """Activa el MFA con un primer código y cierra las demás sesiones. Devuelve un token de
    acceso nuevo para esta sesión, ya con el alcance completo."""
    async with deps.sessions() as db, db.begin():
        user = await db.get(User, who.user_id, with_for_update=True)
        if user.mfa_enabled:
            raise InvalidRequest("El MFA ya está activado")
        await _check_mfa_code(deps, user, code, InvalidRequest)
        user.mfa_enabled = True
        await _revoke_sessions(db, user.id, "mfa_changed", keep=who.session_id)
        token = _access_token(deps, user, who.session_id)
    log.info("auth.mfa_enabled", user_id=who.user_id)
    return token


async def mfa_disable(deps: Deps, who: Principal, password: str, code: str) -> None:
    async with deps.sessions() as db, db.begin():
        user = await db.get(User, who.user_id, with_for_update=True)
        if not user.mfa_enabled:
            raise InvalidRequest("El MFA no está activado")
        if deps.settings.require_mfa_for_admins and user.role == UserRole.ADMIN:
            raise PermissionDenied("Los administradores no pueden desactivar el MFA")
        if not passwords.verify_password(password, user.password_hash):
            raise PermissionDenied("La contraseña no es correcta")
        await _check_mfa_code(deps, user, code, InvalidRequest)
        user.mfa_enabled, user.mfa_secret_encrypted, user.mfa_last_step = False, None, None
        await _revoke_sessions(db, user.id, "mfa_changed", keep=who.session_id)
    log.info("auth.mfa_disabled", user_id=who.user_id)


async def list_sessions(deps: Deps, who: Principal) -> list[UserSession]:
    async with deps.sessions() as db:
        return list((await db.execute(
            select(UserSession).where(UserSession.user_id == who.user_id, UserSession.revoked_at.is_(None),
                                      UserSession.expires_at > utcnow())
            .order_by(UserSession.last_used_at.desc()))).scalars())


async def revoke_session(deps: Deps, who: Principal, session_id: int) -> None:
    async with deps.sessions() as db, db.begin():
        result = await db.execute(update(UserSession).where(
            UserSession.id == session_id, UserSession.user_id == who.user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoked_reason="revoked"))
    if not result.rowcount:
        raise InvalidRequest("No hay ninguna sesión abierta con ese id")


async def revoke_other_sessions(deps: Deps, who: Principal) -> int:
    async with deps.sessions() as db, db.begin():
        return await _revoke_sessions(db, who.user_id, "revoked", keep=who.session_id)


# --- Claves personales

async def personal_application(db, organization_id: int) -> Application | None:
    """La aplicación de la organización cuyas reglas siguen las claves personales."""
    rows = (await db.execute(select(Application).where(
        Application.organization_id == organization_id, Application.personal_keys.is_(True),
        Application.status == ApplicationStatus.ACTIVE, Application.deleted_at.is_(None))
        .order_by(Application.id).limit(2))).scalars().all()
    return rows[0] if len(rows) == 1 else None


def _active_keys(user_id: int):
    now = utcnow()
    return select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.kind == KeyKind.USER, ApiKey.revoked_at.is_(None),
                                (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))


async def list_keys(deps: Deps, who: Principal) -> list[ApiKey]:
    async with deps.sessions() as db:
        return list((await db.execute(_active_keys(who.user_id).order_by(ApiKey.created_at.desc()))).scalars())


async def create_key(deps: Deps, who: Principal, label: str, expires_days: int) -> tuple[ApiKey, str]:
    if not 1 <= expires_days <= PERSONAL_KEY_MAX_DAYS:
        raise InvalidRequest(f"La caducidad debe estar entre 1 y {PERSONAL_KEY_MAX_DAYS} días")
    async with deps.sessions() as db, db.begin():
        # Bloquear a la persona serializa las altas: el máximo de claves no se pasa con dos a la vez
        await db.get(User, who.user_id, with_for_update=True)
        if await personal_application(db, who.organization_id) is None:
            raise PermissionDenied("La organización no tiene activadas las claves personales")
        count = (await db.execute(select(func.count()).select_from(_active_keys(who.user_id).subquery()))).scalar()
        if count >= PERSONAL_KEYS_PER_USER:
            raise InvalidRequest(f"Como mucho {PERSONAL_KEYS_PER_USER} claves activas: revoca una antes")
        new = new_user_key()
        key = ApiKey(organization_id=who.organization_id, kind=KeyKind.USER, user_id=who.user_id, prefix=new.prefix,
                     key_hash=new.key_hash, label=label, created_by=who.user_id,
                     expires_at=utcnow() + timedelta(days=expires_days))
        db.add(key)
        await db.flush()
    log.info("auth.key_created", user_id=who.user_id, key_prefix=key.prefix)
    return key, new.raw


async def revoke_key(deps: Deps, who: Principal, key_id: int) -> None:
    async with deps.sessions() as db, db.begin():
        result = await db.execute(update(ApiKey).where(
            ApiKey.id == key_id, ApiKey.user_id == who.user_id, ApiKey.kind == KeyKind.USER,
            ApiKey.revoked_at.is_(None)).values(revoked_at=utcnow()))
    if not result.rowcount:
        raise InvalidRequest("No tienes ninguna clave activa con ese id")
    log.info("auth.key_revoked", user_id=who.user_id, key_id=key_id)
