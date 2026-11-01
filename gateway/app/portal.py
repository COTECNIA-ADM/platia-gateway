"""Portal de usuario (G14, RF-17): lo que una persona ve de sí misma.

Solo datos propios: su cuenta, los modelos que puede usar con sus claves personales, sus
cuotas con su consumo, su uso (de la auditoría, por `user_id`) y sus últimas peticiones. Nunca
el consumo de otras personas ni el total de la organización. La rotación de claves deja la
anterior viva unas horas para cambiarla sin cortar el servicio (cap. 30).
"""
from datetime import timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select

from . import identity
from .account_api import _bearer
from .admin.common import iso, page, plain
from .admin.rules import used
from .models import (Application, AuditLog, AuditStatus, LimitScope, LlmModel, ModelPrice, Organization, Team,
                     UsageLimit, User)
from .models.base import utcnow
from .pipeline.auth import personal_ref
from .pipeline.runner import deps_of

router = APIRouter(prefix="/me", tags=["portal"])
MAX_DAYS = 90


class RotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_days: int = Field(default=90, ge=1, le=identity.PERSONAL_KEY_MAX_DAYS)
    grace_hours: int = Field(default=24, ge=0, le=identity.ROTATION_MAX_GRACE_HOURS)


async def _me(request: Request) -> identity.Principal:
    return await identity.authenticate(deps_of(request), _bearer(request))


@router.get("/portal")
async def portal(request: Request):
    """Cuenta, aplicación de las claves personales, modelos que puede usar y sus cuotas."""
    who = await _me(request)
    subject = personal_ref(who.user_id)
    async with deps_of(request).sessions() as db:
        user = await db.get(User, who.user_id)
        org = await db.get(Organization, who.organization_id)
        team = await db.get(Team, user.team_id) if user.team_id else None
        app = await identity.personal_application(db, who.organization_id)
        models, limits = [], []
        if app is not None:
            query = select(LlmModel).where(LlmModel.deleted_at.is_(None), LlmModel.is_active.is_(True))
            if app.allowed_models:
                query = query.where(LlmModel.model_key.in_(app.allowed_models))
            rows = (await db.execute(query.order_by(LlmModel.model_key))).scalars().all()
            now = utcnow()
            prices = {}
            for price in (await db.execute(select(ModelPrice).where(
                    ModelPrice.model_id.in_([m.id for m in rows] or [0]), ModelPrice.effective_from <= now)
                    .order_by(ModelPrice.effective_from))).scalars():
                prices[price.model_id] = price
            models = [{"model": m.model_key, "name": m.display_name, "family": str(m.family),
                       "input_per_mtok": plain(prices[m.id].input_per_mtok) if m.id in prices else None,
                       "output_per_mtok": plain(prices[m.id].output_per_mtok) if m.id in prices else None}
                      for m in rows]
            # Solo las cuotas de cada persona en la aplicación de las claves personales: las de
            # equipo u organización dirían cuánto gastan los demás
            for limit in (await db.execute(select(UsageLimit).where(
                    UsageLimit.scope == LimitScope.END_USER, UsageLimit.scope_id == app.id,
                    UsageLimit.is_active.is_(True), UsageLimit.deleted_at.is_(None),
                    or_(UsageLimit.subject.is_(None), UsageLimit.subject == subject)))).scalars():
                spent = await used(db, limit, subject)
                limits.append({"metric": str(limit.metric), "period": str(limit.period),
                               "value": plain(limit.limit_value), "alert_pct": limit.alert_pct,
                               "used": plain(spent) if spent is not None else None,
                               "only_you": limit.subject == subject})
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name, "role": str(user.role),
                 "team": team.name if team else None, "organization": org.name, "mfa_enabled": user.mfa_enabled,
                 "password_changed_at": iso(user.password_changed_at), "last_login_at": iso(user.last_login_at)},
        "personal_keys": {"enabled": app is not None, "max_keys": identity.PERSONAL_KEYS_PER_USER,
                          "max_days": identity.PERSONAL_KEY_MAX_DAYS,
                          "max_grace_hours": identity.ROTATION_MAX_GRACE_HOURS,
                          "purposes": (app.allowed_purposes or []) if app else []},
        "models": models,
        "limits": limits,
    }


@router.get("/usage")
async def usage(request: Request, days: int = Query(30, ge=1, le=MAX_DAYS)):
    """Consumo propio: totales, por modelo y por día (UTC)."""
    who = await _me(request)
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    scope = [AuditLog.organization_id == who.organization_id, AuditLog.user_id == who.user_id,
             AuditLog.created_at >= since]
    errors = func.sum(case((AuditLog.status == AuditStatus.ERROR, 1), else_=0))
    blocked = func.sum(case((AuditLog.status == AuditStatus.BLOCKED, 1), else_=0))
    tokens = func.sum(AuditLog.input_tokens + AuditLog.output_tokens)
    async with deps_of(request).sessions() as db:
        total = (await db.execute(select(func.count(), errors, blocked, func.sum(AuditLog.input_tokens),
                                         func.sum(AuditLog.output_tokens), func.sum(AuditLog.cost_usd))
                                  .where(*scope))).one()
        by_model = (await db.execute(select(AuditLog.model_key, func.count(), tokens, func.sum(AuditLog.cost_usd))
                                     .where(*scope).group_by(AuditLog.model_key)
                                     .order_by(func.count().desc()))).all()
        day = func.date_format(AuditLog.created_at, "%Y-%m-%d")
        series = (await db.execute(select(day, func.count(), errors, func.sum(AuditLog.cost_usd))
                                   .where(*scope).group_by(day).order_by(day))).all()
    return {
        "days": days, "since": iso(since),
        "totals": {"requests": total[0], "errors": int(total[1] or 0), "blocked": int(total[2] or 0),
                   "input_tokens": int(total[3] or 0), "output_tokens": int(total[4] or 0),
                   "cost_usd": plain(total[5] or 0)},
        "by_model": [{"model": m or "-", "requests": n, "tokens": int(t or 0), "cost_usd": plain(c or 0)}
                     for m, n, t, c in by_model],
        "series": [{"day": d, "requests": n, "errors": int(e or 0), "cost_usd": plain(c or 0)}
                   for d, n, e, c in series],
    }


@router.get("/requests")
async def requests(request: Request, before_id: int | None = None, limit: int = Query(50, ge=1, le=200)):
    """Últimas peticiones propias. Sin texto: no se guarda (ADR-013)."""
    who = await _me(request)
    query = select(AuditLog, Application.slug).outerjoin(Application, Application.id == AuditLog.application_id) \
        .where(AuditLog.organization_id == who.organization_id, AuditLog.user_id == who.user_id)
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(query.order_by(AuditLog.id.desc()).limit(limit + 1))).all()
    return page([{"id": r.id, "request_id": r.request_id, "created_at": iso(r.created_at), "application": slug,
                  "purpose": r.purpose, "model": r.model_key or r.requested_model, "status": str(r.status),
                  "http_status": r.http_status, "error_type": r.error_type, "input_tokens": r.input_tokens,
                  "output_tokens": r.output_tokens, "cost_usd": plain(r.cost_usd), "latency_ms": r.latency_ms,
                  "api_key_prefix": r.api_key_prefix} for r, slug in rows], limit)


@router.post("/keys/{key_id}/rotate", status_code=201)
async def rotate_key(request: Request, key_id: int, body: RotateIn):
    who = await _me(request)
    key, raw, old = await identity.rotate_key(deps_of(request), who, key_id, body.expires_days, body.grace_hours)
    return JSONResponse({"id": key.id, "prefix": key.prefix, "label": key.label, "created_at": iso(key.created_at),
                         "expires_at": iso(key.expires_at), "last_used_at": None, "key": raw,
                         "previous": {"id": old.id, "expires_at": iso(old.expires_at),
                                      "revoked_at": iso(old.revoked_at)}},
                        status_code=201, headers={"Cache-Control": "no-store"})
