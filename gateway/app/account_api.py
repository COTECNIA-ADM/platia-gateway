"""Rutas de identidad de personas (G10, ADR-014): `/auth/*` (sesión, MFA) y `/me/*`
(cuenta y claves personales). Las usarán el panel (G13) y el portal (G14).

- El token de acceso va en `Authorization: Bearer` y vive en memoria del navegador.
- La renovación va en una cookie HttpOnly, `SameSite=Strict`, solo para `/auth`. Las rutas
  que la usan exigen además la cabecera `X-Requested-With` (un formulario de otra web no
  puede enviarla sin permiso CORS).
- Los errores tienen la forma de los de la API de Anthropic, como el resto del gateway.
"""
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import identity
from .errors import GatewayError, InvalidRequest, error_body
from .models import Contract
from .pipeline.runner import deps_of, request_id_of

log = structlog.get_logger(__name__)
auth_router = APIRouter(prefix="/auth", tags=["sesión"])
me_router = APIRouter(prefix="/me", tags=["cuenta"])

COOKIE = "platia_gw_refresh"
COOKIE_PATH = "/auth"
CSRF_HEADER = "x-requested-with"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginIn(_Body):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    organization: str | None = Field(default=None, max_length=64)


class MfaLoginIn(_Body):
    mfa_ticket: str = Field(min_length=1, max_length=2048)
    code: str = Field(min_length=6, max_length=10)


class PasswordIn(_Body):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class CodeIn(_Body):
    code: str = Field(min_length=6, max_length=10)


class MfaDisableIn(_Body):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=10)


class KeyIn(_Body):
    label: str = Field(default="", max_length=255)
    expires_days: int = Field(default=90, ge=1, le=identity.PERSONAL_KEY_MAX_DAYS)


# --- Utilidades

def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if value else None


def _ip(request: Request) -> str | None:
    # Solo Nginx llega al gateway (red edge_net): su X-Real-IP es la dirección del cliente
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value[:7].lower() == "bearer " else None


def _require_csrf_header(request: Request) -> None:
    if not request.headers.get(CSRF_HEADER):
        raise InvalidRequest("Falta la cabecera X-Requested-With")


def _session_body(tokens: identity.Tokens) -> dict:
    return {"access_token": tokens.access_token, "token_type": "bearer", "expires_in": tokens.expires_in,
            "scope": tokens.scope}


def _with_cookie(request: Request, body: dict, refresh_token: str, status: int = 200) -> JSONResponse:
    settings = deps_of(request).settings
    response = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE, refresh_token, max_age=settings.session_max_days * 86400, path=COOKIE_PATH,
                        httponly=True, secure=settings.cookie_secure, samesite="strict")
    return response


def _clear_cookie(response: Response) -> Response:
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    return response


async def _principal(request: Request, *, allow_setup: bool = False) -> identity.Principal:
    return await identity.authenticate(deps_of(request), _bearer(request), allow_setup=allow_setup)


def _user_body(who: identity.Principal) -> dict:
    return {"id": who.user_id, "email": who.email, "name": who.name, "role": str(who.role),
            "organization_id": who.organization_id, "mfa_enabled": who.mfa_enabled, "scope": who.scope}


# --- /auth

@auth_router.post("/login")
async def login(request: Request, body: LoginIn):
    result = await identity.login(deps_of(request), body.email, body.password, body.organization, _ip(request),
                                  request.headers.get("user-agent"))
    if result.mfa_ticket:
        return JSONResponse({"mfa_required": True, "mfa_ticket": result.mfa_ticket,
                             "expires_in": identity.MFA_TICKET_SECONDS}, headers={"Cache-Control": "no-store"})
    return _with_cookie(request, _session_body(result.tokens), result.tokens.refresh_token)


@auth_router.post("/login/mfa")
async def login_mfa(request: Request, body: MfaLoginIn):
    tokens = await identity.complete_mfa(deps_of(request), body.mfa_ticket, body.code, _ip(request),
                                         request.headers.get("user-agent"))
    return _with_cookie(request, _session_body(tokens), tokens.refresh_token)


@auth_router.post("/refresh")
async def refresh(request: Request):
    _require_csrf_header(request)
    try:
        tokens = await identity.refresh(deps_of(request), request.cookies.get(COOKIE))
    except GatewayError as exc:
        if exc.status == 401:
            return _clear_cookie(_error(request, exc))
        raise
    return _with_cookie(request, _session_body(tokens), tokens.refresh_token)


@auth_router.post("/logout", status_code=204)
async def logout(request: Request):
    _require_csrf_header(request)
    deps = deps_of(request)
    session_id = None
    if _bearer(request):
        try:
            session_id = (await identity.authenticate(deps, _bearer(request), allow_setup=True)).session_id
        except GatewayError:
            session_id = None
    await identity.logout(deps, session_id=session_id, refresh_token=request.cookies.get(COOKIE))
    return _clear_cookie(Response(status_code=204))


@auth_router.get("/me")
async def me(request: Request):
    return _user_body(await _principal(request, allow_setup=True))


@auth_router.post("/mfa/setup")
async def mfa_setup(request: Request):
    who = await _principal(request, allow_setup=True)
    secret, uri = await identity.mfa_setup(deps_of(request), who)
    return JSONResponse({"secret": secret, "otpauth_uri": uri}, headers={"Cache-Control": "no-store"})


@auth_router.post("/mfa/enable")
async def mfa_enable(request: Request, body: CodeIn):
    who = await _principal(request, allow_setup=True)
    token, seconds, scope = await identity.mfa_enable(deps_of(request), who, body.code)
    return {"access_token": token, "token_type": "bearer", "expires_in": seconds, "scope": scope}


@auth_router.post("/mfa/disable", status_code=204)
async def mfa_disable(request: Request, body: MfaDisableIn):
    who = await _principal(request)
    await identity.mfa_disable(deps_of(request), who, body.password, body.code)
    return Response(status_code=204)


@auth_router.post("/password")
async def change_password(request: Request, body: PasswordIn):
    who = await _principal(request, allow_setup=True)
    closed = await identity.change_password(deps_of(request), who, body.current_password, body.new_password)
    return {"other_sessions_closed": closed}


@auth_router.get("/sessions")
async def sessions(request: Request):
    who = await _principal(request)
    rows = await identity.list_sessions(deps_of(request), who)
    return {"data": [{"id": s.id, "current": s.id == who.session_id, "created_at": _iso(s.created_at),
                      "last_used_at": _iso(s.last_used_at), "expires_at": _iso(s.expires_at),
                      "ip_address": s.ip_address, "user_agent": s.user_agent} for s in rows]}


@auth_router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(request: Request, session_id: int):
    who = await _principal(request)
    await identity.revoke_session(deps_of(request), who, session_id)
    return Response(status_code=204)


@auth_router.post("/sessions/revoke-others")
async def revoke_others(request: Request):
    who = await _principal(request)
    return {"sessions_closed": await identity.revoke_other_sessions(deps_of(request), who)}


# --- /me: claves personales

def _key_body(key) -> dict:
    return {"id": key.id, "prefix": key.prefix, "label": key.label, "created_at": _iso(key.created_at),
            "expires_at": _iso(key.expires_at), "last_used_at": _iso(key.last_used_at)}


@me_router.get("/keys")
async def list_keys(request: Request):
    who = await _principal(request)
    return {"data": [_key_body(k) for k in await identity.list_keys(deps_of(request), who)]}


@me_router.post("/keys", status_code=201)
async def create_key(request: Request, body: KeyIn):
    who = await _principal(request)
    key, raw = await identity.create_key(deps_of(request), who, body.label, body.expires_days)
    # La clave en claro se enseña esta única vez
    return JSONResponse({**_key_body(key), "key": raw}, status_code=201, headers={"Cache-Control": "no-store"})


@me_router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(request: Request, key_id: int):
    who = await _principal(request)
    await identity.revoke_key(deps_of(request), who, key_id)
    return Response(status_code=204)


# --- Errores

def _error(request: Request, exc: GatewayError) -> JSONResponse:
    request_id = request_id_of(request)
    headers = {"X-Request-ID": request_id, **exc.headers}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after)))
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(error_body(Contract.ANTHROPIC, exc, request_id), status_code=exc.status,
                        headers=headers)


def install_error_handlers(app) -> None:
    """Errores de estas rutas en JSON, sin detalles internos; los demás siguen igual."""

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError):
        return _error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "cuerpo" for e in exc.errors()})
        return _error(request, InvalidRequest(f"Petición no válida: revisa {', '.join(fields)}"))
