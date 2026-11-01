"""Equipos, aplicaciones y sus claves desde el panel (G13, RF-16)."""
import re
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from sqlalchemy import func, select

from ..errors import InvalidRequest, NotFound
from ..identity import Principal
from ..keys import new_application_key
from ..models import ApiKey, Application, ApplicationStatus, KeyKind, LlmModel, Team, User
from ..models.base import utcnow
from ..pipeline.runner import deps_of
from .common import Body, admin, commit, flush, get_owned, iso, record

router = APIRouter(tags=["panel: aplicaciones"])

SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
PURPOSE = r"^(\*|[A-Za-z0-9][A-Za-z0-9_.:-]{0,63})$"
MAX_CACHE_TTL = 30 * 86400


class TeamIn(Body):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=255)


class TeamPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None


class ApplicationIn(Body):
    team_id: int
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=255)
    trusted: bool = False
    allowed_purposes: list[str] = Field(default_factory=list, max_length=100)
    allowed_models: list[str] | None = Field(default=None, max_length=100)
    personal_keys: bool = False


class ApplicationPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    team_id: int | None = None
    status: ApplicationStatus | None = None
    trusted: bool | None = None
    allowed_purposes: list[str] | None = Field(default=None, max_length=100)
    # null: todos los modelos que permita la organización (hay que enviarlo expresamente)
    allowed_models: list[str] | None = Field(default=None, max_length=100)
    personal_keys: bool | None = None
    cache_ttl_seconds: int | None = Field(default=None, ge=0, le=MAX_CACHE_TTL)
    cache_purposes: list[str] | None = Field(default=None, max_length=100)


class KeyIn(Body):
    label: str = Field(default="", max_length=255)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


def _team_body(team: Team) -> dict:
    return {"id": team.id, "slug": team.slug, "name": team.name, "is_active": team.is_active,
            "created_at": iso(team.created_at)}


def _app_body(app: Application, keys: int = 0, last_used=None) -> dict:
    return {"id": app.id, "slug": app.slug, "name": app.name, "team_id": app.team_id, "status": str(app.status),
            "trusted": app.trusted, "allowed_purposes": app.allowed_purposes, "allowed_models": app.allowed_models,
            "personal_keys": app.personal_keys, "cache_ttl_seconds": app.cache_ttl_seconds,
            "cache_purposes": app.cache_purposes, "active_keys": keys, "last_used_at": iso(last_used),
            "created_at": iso(app.created_at), "updated_at": iso(app.updated_at)}


def key_body(key: ApiKey, owner: str | None = None) -> dict:
    now = utcnow()
    state = "revoked" if key.revoked_at else "expired" if key.expires_at and key.expires_at <= now else "active"
    return {"id": key.id, "kind": str(key.kind), "prefix": key.prefix, "label": key.label, "state": state,
            "application_id": key.application_id, "user_id": key.user_id, "owner": owner,
            "created_at": iso(key.created_at), "expires_at": iso(key.expires_at), "revoked_at": iso(key.revoked_at),
            "last_used_at": iso(key.last_used_at)}


def _purposes(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    clean = list(dict.fromkeys(v.strip() for v in values if v.strip()))
    bad = [v for v in clean if not re.match(PURPOSE, v)]
    if bad:
        raise InvalidRequest(f"Propósitos no válidos: {', '.join(bad)}")
    return clean


async def _models(db, values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    clean = list(dict.fromkeys(values))
    known = set((await db.execute(select(LlmModel.model_key).where(
        LlmModel.model_key.in_(clean), LlmModel.deleted_at.is_(None)))).scalars())
    missing = [m for m in clean if m not in known]
    if missing:
        raise InvalidRequest(f"Modelos que no están en el catálogo: {', '.join(missing)}")
    return clean


async def _check_personal(db, organization_id: int, exclude: int | None = None) -> None:
    query = select(Application.id).where(Application.organization_id == organization_id,
                                         Application.personal_keys.is_(True), Application.deleted_at.is_(None))
    if exclude is not None:
        query = query.where(Application.id != exclude)
    if (await db.execute(query)).first():
        raise InvalidRequest("La organización ya tiene una aplicación para las claves personales")


# --- Equipos

@router.get("/teams")
async def list_teams(request: Request, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(select(Team).where(Team.organization_id == who.organization_id,
                                                    Team.deleted_at.is_(None)).order_by(Team.slug))).scalars()
        return {"data": [_team_body(t) for t in rows]}


@router.post("/teams", status_code=201)
async def create_team(request: Request, body: TeamIn, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        team = Team(organization_id=who.organization_id, slug=body.slug, name=body.name)
        db.add(team)
        await flush(db, f"Ya existe un equipo '{body.slug}'")
        record(db, request, who, "team.create", "team", team.id, body.model_dump())
        await commit(db, f"Ya existe un equipo '{body.slug}'")
        return _team_body(team)


@router.patch("/teams/{team_id}")
async def update_team(request: Request, team_id: int, body: TeamPatch, who: Principal = Depends(admin)):
    changes = body.model_dump(exclude_unset=True)
    async with deps_of(request).sessions() as db:
        team = await get_owned(db, Team, team_id, who.organization_id, "el equipo")
        for field, value in changes.items():
            if value is None:
                raise InvalidRequest(f"'{field}' no puede ser nulo")
            setattr(team, field, value)
        record(db, request, who, "team.update", "team", team.id, changes)
        await commit(db)
        return _team_body(team)


# --- Aplicaciones

async def _app_stats(db, organization_id: int) -> dict[int, tuple[int, object]]:
    now = utcnow()
    rows = await db.execute(select(ApiKey.application_id, func.count(), func.max(ApiKey.last_used_at)).where(
        ApiKey.organization_id == organization_id, ApiKey.kind == KeyKind.APPLICATION, ApiKey.revoked_at.is_(None),
        (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now)).group_by(ApiKey.application_id))
    return {app_id: (count, last) for app_id, count, last in rows}


@router.get("/applications")
async def list_applications(request: Request, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(select(Application).where(
            Application.organization_id == who.organization_id, Application.deleted_at.is_(None)
        ).order_by(Application.slug))).scalars().all()
        stats = await _app_stats(db, who.organization_id)
        return {"data": [_app_body(a, *stats.get(a.id, (0, None))) for a in rows]}


@router.get("/applications/{app_id}")
async def get_application(request: Request, app_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        app = await get_owned(db, Application, app_id, who.organization_id, "la aplicación")
        stats = await _app_stats(db, who.organization_id)
        return _app_body(app, *stats.get(app.id, (0, None)))


@router.post("/applications", status_code=201)
async def create_application(request: Request, body: ApplicationIn, who: Principal = Depends(admin)):
    if body.personal_keys and body.trusted:
        raise InvalidRequest("La aplicación de las claves personales no puede ser de confianza")
    async with deps_of(request).sessions() as db:
        await get_owned(db, Team, body.team_id, who.organization_id, "el equipo")
        if body.personal_keys:
            await _check_personal(db, who.organization_id)
        app = Application(organization_id=who.organization_id, team_id=body.team_id, slug=body.slug, name=body.name,
                          trusted=body.trusted, allowed_purposes=_purposes(body.allowed_purposes),
                          allowed_models=await _models(db, body.allowed_models), personal_keys=body.personal_keys)
        db.add(app)
        await flush(db, f"Ya existe una aplicación '{body.slug}'")
        record(db, request, who, "application.create", "application", app.id, body.model_dump())
        await commit(db, f"Ya existe una aplicación '{body.slug}'")
        return _app_body(app)


@router.patch("/applications/{app_id}")
async def update_application(request: Request, app_id: int, body: ApplicationPatch, who: Principal = Depends(admin)):
    changes = body.model_dump(exclude_unset=True)
    nullable = {"allowed_models", "cache_purposes"}
    for field, value in changes.items():
        if value is None and field not in nullable:
            raise InvalidRequest(f"'{field}' no puede ser nulo")
    async with deps_of(request).sessions() as db:
        app = await get_owned(db, Application, app_id, who.organization_id, "la aplicación")
        if "team_id" in changes:
            await get_owned(db, Team, changes["team_id"], who.organization_id, "el equipo")
        if "allowed_purposes" in changes:
            changes["allowed_purposes"] = _purposes(changes["allowed_purposes"])
        if "cache_purposes" in changes:
            changes["cache_purposes"] = _purposes(changes["cache_purposes"]) or None
        if "allowed_models" in changes:
            changes["allowed_models"] = await _models(db, changes["allowed_models"])
        personal = changes.get("personal_keys", app.personal_keys)
        trusted = changes.get("trusted", app.trusted)
        if personal and trusted:
            raise InvalidRequest("La aplicación de las claves personales no puede ser de confianza")
        if personal and not app.personal_keys:
            await _check_personal(db, who.organization_id, exclude=app.id)
        for field, value in changes.items():
            setattr(app, field, value)
        record(db, request, who, "application.update", "application", app.id, changes)
        await commit(db)
        stats = await _app_stats(db, who.organization_id)
        return _app_body(app, *stats.get(app.id, (0, None)))


# --- Claves

@router.get("/applications/{app_id}/keys")
async def list_app_keys(request: Request, app_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        app = await get_owned(db, Application, app_id, who.organization_id, "la aplicación")
        rows = (await db.execute(select(ApiKey).where(ApiKey.application_id == app.id)
                                 .order_by(ApiKey.id.desc()))).scalars()
        return {"data": [key_body(k, app.slug) for k in rows]}


@router.post("/applications/{app_id}/keys", status_code=201)
async def create_app_key(request: Request, app_id: int, body: KeyIn, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        app = await get_owned(db, Application, app_id, who.organization_id, "la aplicación")
        if app.status != ApplicationStatus.ACTIVE:
            raise InvalidRequest(f"La aplicación '{app.slug}' no está activa")
        new = new_application_key(app.slug)
        key = ApiKey(organization_id=who.organization_id, kind=KeyKind.APPLICATION, application_id=app.id,
                     prefix=new.prefix, key_hash=new.key_hash, label=body.label, created_by=who.user_id,
                     expires_at=utcnow() + timedelta(days=body.expires_days) if body.expires_days else None)
        db.add(key)
        await flush(db)
        record(db, request, who, "key.create", "api_key", key.id, {"application_id": app.id, "prefix": new.prefix,
                                                                   "label": body.label,
                                                                   "expires_days": body.expires_days})
        await commit(db)
        # La clave en claro se enseña esta única vez
        return JSONResponse({**key_body(key, app.slug), "key": new.raw}, status_code=201,
                            headers={"Cache-Control": "no-store"})


@router.get("/keys")
async def list_keys(request: Request, kind: Literal["application", "user"] | None = None,
                    state: Literal["active", "all"] = "active", who: Principal = Depends(admin)):
    """Todas las claves de la organización, también las personales (sin el secreto)."""
    now = utcnow()
    query = select(ApiKey, Application.slug, User.email).outerjoin(
        Application, Application.id == ApiKey.application_id).outerjoin(User, User.id == ApiKey.user_id).where(
        ApiKey.organization_id == who.organization_id)
    if kind:
        query = query.where(ApiKey.kind == kind)
    if state == "active":
        query = query.where(ApiKey.revoked_at.is_(None), (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(query.order_by(ApiKey.id.desc()).limit(1000))).all()
        return {"data": [key_body(k, slug or email) for k, slug, email in rows]}


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(request: Request, key_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        key = await db.get(ApiKey, key_id)
        if key is None or key.organization_id != who.organization_id:
            raise NotFound(f"No existe la clave {key_id}")
        if key.revoked_at is None:
            key.revoked_at = utcnow()
            record(db, request, who, "key.revoke", "api_key", key.id, {"prefix": key.prefix, "kind": key.kind})
            await commit(db)
    return Response(status_code=204)
