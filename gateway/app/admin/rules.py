"""Cuotas, límites de ritmo y política de contenido desde el panel (G13; reglas de G7 y G8).

Las mismas reglas que la CLI: el gasto se limita por día o mes, las simultáneas no tienen
periodo, un límite por ámbito, métrica y periodo (si existe, se actualiza), y en cada casilla
de contenido solo valen las acciones que tienen sentido (`content_policy.ALLOWED`).
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import func, or_, select

from ..content_policy import ALLOWED, DEFAULTS
from ..errors import InvalidRequest, NotFound
from ..identity import Principal
from ..limits import window
from ..models import (Application, AuditLog, AuditStatus, ContentAction, ContentCheck, ContentPolicy, Direction,
                      LimitMetric, LimitPeriod, LimitScope, PolicyScope, Team, UsageLimit)
from ..models.base import utcnow
from ..pipeline.runner import deps_of
from .common import Body, admin, commit, flush, get_owned, iso, plain, record

router = APIRouter(tags=["panel: reglas"])


class LimitIn(Body):
    scope: LimitScope
    # La organización no lo necesita (es la de quien cambia); equipo y aplicación, sí.
    # Para personas, la aplicación cuya persona final se limita
    scope_id: int | None = None
    # Solo personas: una concreta (su X-Platia-User-Id o usr:<id>); sin ella, cada persona
    subject: str | None = Field(default=None, min_length=1, max_length=128)
    metric: LimitMetric
    period: LimitPeriod | None = None
    value: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    alert_pct: int = Field(default=80, ge=1, le=100)


class LimitPatch(Body):
    value: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)
    alert_pct: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None


class PolicyIn(Body):
    application_id: int | None = None
    direction: Direction
    check: ContentCheck
    # Una acción, o "default" para volver a lo heredado
    action: str = Field(min_length=2, max_length=16)


def _limit_body(limit: UsageLimit, label: str, used: Decimal | None) -> dict:
    return {"id": limit.id, "scope": str(limit.scope), "scope_id": limit.scope_id, "scope_label": label,
            "subject": limit.subject, "metric": str(limit.metric), "period": str(limit.period),
            "value": plain(limit.limit_value), "alert_pct": limit.alert_pct, "is_active": limit.is_active,
            "used": plain(used) if used is not None else None, "updated_at": iso(limit.updated_at)}


async def _used(db, limit: UsageLimit) -> Decimal | None:
    """Consumo del periodo actual según la auditoría (día y mes; el de minuto vive solo en Redis)."""
    if limit.period not in (LimitPeriod.DAY, LimitPeriod.MONTH):
        return None
    if limit.scope == LimitScope.END_USER and not limit.subject:
        return None  # el de cada persona por separado: no hay un único consumo
    column = {LimitScope.ORGANIZATION: AuditLog.organization_id, LimitScope.TEAM: AuditLog.team_id}.get(
        limit.scope, AuditLog.application_id)
    where = [column == limit.scope_id, AuditLog.created_at >= window(limit.period, utcnow()).start,
             AuditLog.status != AuditStatus.BLOCKED]
    if limit.subject:
        where.append(AuditLog.end_user_ref == limit.subject)
    measure = {LimitMetric.COST_USD: func.sum(AuditLog.cost_usd), LimitMetric.REQUESTS: func.count(),
               LimitMetric.TOKENS: func.sum(AuditLog.input_tokens + AuditLog.output_tokens
                                            + AuditLog.cache_read_tokens + AuditLog.cache_write_tokens)}
    return Decimal((await db.execute(select(measure[limit.metric]).where(*where))).scalar() or 0)


async def _scopes(db, organization_id: int) -> tuple[dict[int, str], dict[int, str]]:
    teams = dict((await db.execute(select(Team.id, Team.slug).where(Team.organization_id == organization_id,
                                                                    Team.deleted_at.is_(None)))).all())
    apps = dict((await db.execute(select(Application.id, Application.slug).where(
        Application.organization_id == organization_id, Application.deleted_at.is_(None)))).all())
    return teams, apps


def _org_filter(organization_id: int, teams: dict, apps: dict):
    return or_((UsageLimit.scope == LimitScope.ORGANIZATION) & (UsageLimit.scope_id == organization_id),
               (UsageLimit.scope == LimitScope.TEAM) & UsageLimit.scope_id.in_(list(teams) or [0]),
               UsageLimit.scope.in_([LimitScope.APPLICATION, LimitScope.END_USER])
               & UsageLimit.scope_id.in_(list(apps) or [0]))


def _label(limit: UsageLimit, teams: dict, apps: dict) -> str:
    if limit.scope == LimitScope.ORGANIZATION:
        return "organización"
    if limit.scope == LimitScope.TEAM:
        return f"equipo {teams.get(limit.scope_id, limit.scope_id)}"
    app = apps.get(limit.scope_id, limit.scope_id)
    if limit.scope == LimitScope.APPLICATION:
        return f"aplicación {app}"
    return f"persona {limit.subject} de {app}" if limit.subject else f"cada persona de {app}"


async def _owned_limit(db, limit_id: int, organization_id: int) -> tuple[UsageLimit, dict, dict]:
    teams, apps = await _scopes(db, organization_id)
    limit = (await db.execute(select(UsageLimit).where(UsageLimit.id == limit_id, UsageLimit.deleted_at.is_(None),
                                                       _org_filter(organization_id, teams, apps)))).scalar()
    if limit is None:
        raise NotFound(f"No existe el límite {limit_id}")
    return limit, teams, apps


@router.get("/limits")
async def list_limits(request: Request, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        teams, apps = await _scopes(db, who.organization_id)
        rows = (await db.execute(select(UsageLimit).where(UsageLimit.deleted_at.is_(None),
                                                          _org_filter(who.organization_id, teams, apps))
                                 .order_by(UsageLimit.id))).scalars().all()
        return {"data": [_limit_body(r, _label(r, teams, apps), await _used(db, r)) for r in rows]}


@router.put("/limits")
async def set_limit(request: Request, body: LimitIn, who: Principal = Depends(admin)):
    """Crea el límite, o cambia el que ya hay para el mismo ámbito, métrica y periodo."""
    if body.metric == LimitMetric.CONCURRENT:
        period = LimitPeriod.NONE
    elif body.period in (None, LimitPeriod.NONE):
        raise InvalidRequest("Falta el periodo (minute, day o month)")
    elif body.metric == LimitMetric.COST_USD and body.period == LimitPeriod.MINUTE:
        raise InvalidRequest("El gasto se limita por día o por mes")
    else:
        period = body.period
    if body.subject and body.scope != LimitScope.END_USER:
        raise InvalidRequest("Solo un límite de personas puede nombrar a una persona")
    async with deps_of(request).sessions() as db:
        if body.scope == LimitScope.ORGANIZATION:
            scope_id = who.organization_id
        elif body.scope_id is None:
            raise InvalidRequest("Falta scope_id (el equipo o la aplicación)")
        else:
            model, what = (Team, "el equipo") if body.scope == LimitScope.TEAM else (Application, "la aplicación")
            scope_id = (await get_owned(db, model, body.scope_id, who.organization_id, what)).id
        existing = (await db.execute(select(UsageLimit).where(
            UsageLimit.scope == body.scope, UsageLimit.scope_id == scope_id, UsageLimit.metric == body.metric,
            UsageLimit.period == period, UsageLimit.deleted_at.is_(None),
            UsageLimit.subject.is_(None) if body.subject is None else UsageLimit.subject == body.subject))).scalar()
        if existing is None:
            existing = UsageLimit(scope=body.scope, scope_id=scope_id, subject=body.subject, metric=body.metric,
                                  period=period, limit_value=body.value, alert_pct=body.alert_pct)
            db.add(existing)
            action = "limit.create"
        else:
            existing.limit_value, existing.alert_pct, existing.is_active = body.value, body.alert_pct, True
            action = "limit.update"
        await flush(db)
        record(db, request, who, action, "limit", existing.id, {**body.model_dump(), "scope_id": scope_id,
                                                                 "period": period})
        await commit(db)
        teams, apps = await _scopes(db, who.organization_id)
        return _limit_body(existing, _label(existing, teams, apps), await _used(db, existing))


@router.patch("/limits/{limit_id}")
async def update_limit(request: Request, limit_id: int, body: LimitPatch, who: Principal = Depends(admin)):
    changes = body.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise InvalidRequest("Los campos no pueden ser nulos")
    async with deps_of(request).sessions() as db:
        limit, teams, apps = await _owned_limit(db, limit_id, who.organization_id)
        if "value" in changes:
            limit.limit_value = changes["value"]
        if "alert_pct" in changes:
            limit.alert_pct = changes["alert_pct"]
        if "is_active" in changes:
            limit.is_active = changes["is_active"]
        record(db, request, who, "limit.update", "limit", limit.id, changes)
        await commit(db)
        return _limit_body(limit, _label(limit, teams, apps), await _used(db, limit))


@router.delete("/limits/{limit_id}", status_code=204)
async def delete_limit(request: Request, limit_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        limit, _, _ = await _owned_limit(db, limit_id, who.organization_id)
        limit.deleted_at, limit.is_active = utcnow(), False
        record(db, request, who, "limit.delete", "limit", limit.id)
        await commit(db)
    return Response(status_code=204)


# --- Política de contenido

async def _policy_rows(db, scope: PolicyScope, scope_id: int) -> dict:
    rows = (await db.execute(select(ContentPolicy).where(ContentPolicy.scope == scope, ContentPolicy.scope_id == scope_id,
                                                         ContentPolicy.deleted_at.is_(None)))).scalars()
    return {(r.direction, r.check_name): r.action for r in rows}


@router.get("/content-policies")
async def list_policies(request: Request, application_id: int | None = None, who: Principal = Depends(admin)):
    """La política efectiva casilla a casilla y de dónde sale: por defecto, organización o aplicación."""
    async with deps_of(request).sessions() as db:
        org_rows = await _policy_rows(db, PolicyScope.ORGANIZATION, who.organization_id)
        app_rows = {}
        if application_id is not None:
            app = await get_owned(db, Application, application_id, who.organization_id, "la aplicación")
            app_rows = await _policy_rows(db, PolicyScope.APPLICATION, app.id)
    cells = []
    for key, default in DEFAULTS.items():
        direction, check = key
        org_value, app_value = org_rows.get(key), app_rows.get(key)
        effective, origin = (app_value, "application") if app_value else (org_value, "organization") if org_value \
            else (default, "default")
        cells.append({"direction": str(direction), "check": str(check), "default": str(default),
                      "organization": plain(org_value), "application": plain(app_value),
                      "effective": str(effective), "origin": origin,
                      "allowed": sorted(str(a) for a in ALLOWED[key])})
    return {"application_id": application_id, "data": cells}


@router.put("/content-policies")
async def set_policy(request: Request, body: PolicyIn, who: Principal = Depends(admin)):
    key = (body.direction, body.check)
    if key not in ALLOWED:
        raise InvalidRequest(f"En la {'salida' if body.direction == Direction.OUTPUT else 'entrada'} no se "
                             f"comprueba '{body.check}'")
    if body.action != "default":
        try:
            action = ContentAction(body.action)
        except ValueError as exc:
            raise InvalidRequest(f"Acción no válida: {body.action}") from exc
        if action not in ALLOWED[key]:
            raise InvalidRequest(f"'{body.action}' no vale para {body.check}: usa "
                                 f"{', '.join(sorted(ALLOWED[key]))} o default")
    async with deps_of(request).sessions() as db:
        if body.application_id is not None:
            app = await get_owned(db, Application, body.application_id, who.organization_id, "la aplicación")
            scope, scope_id = PolicyScope.APPLICATION, app.id
        else:
            scope, scope_id = PolicyScope.ORGANIZATION, who.organization_id
        row = (await db.execute(select(ContentPolicy).filter_by(scope=scope, scope_id=scope_id,
                                                                direction=body.direction,
                                                                check_name=body.check))).scalar_one_or_none()
        if body.action == "default":
            if row is not None and row.deleted_at is None:
                row.deleted_at = utcnow()
        elif row is None:
            db.add(ContentPolicy(scope=scope, scope_id=scope_id, direction=body.direction, check_name=body.check,
                                 action=body.action))
        else:  # una sola fila por casilla: se reutiliza aunque estuviera borrada
            row.action, row.deleted_at = body.action, None
        record(db, request, who, "content_policy.set", "content_policy",
               f"{scope}:{scope_id}:{body.direction}.{body.check}", body.model_dump())
        await commit(db)
    return await list_policies(request, body.application_id, who)
