"""Operaciones y auditoría en el panel: resumen, consulta de la auditoría y registro de cambios (G13).

Todo sale de MySQL y solo de la organización de quien pregunta. Nunca hay texto de prompts ni
respuestas (no se guarda): la auditoría da la huella del prompt, tokens, coste, latencias y
lo que encontró la seguridad de contenido. El diagnóstico de una petición sigue
machinebooks.ai/ES/aigateway/cap-28/block-12.py y el resumen, block-02.py (MIT).
"""
import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select

from .. import audit_chain
from ..errors import InvalidRequest, NotFound
from ..identity import Principal
from ..models import AdminAction, Application, AuditChainState, AuditLog, AuditStatus
from ..models.base import utcnow
from ..pipeline.runner import deps_of
from .common import admin, iso, operator, page, plain

router = APIRouter(tags=["panel: operaciones"])

MAX_HOURS = 24 * 90
EXPORT_MAX_ROWS = 100_000
# Para el p95 del resumen basta una muestra grande de lo más reciente
OVERHEAD_SAMPLE = 20_000


def _percentile(values: list[int], pct: int) -> int | None:
    if not values:
        return None
    values = sorted(values)
    index = max(0, min(len(values) - 1, -(-len(values) * pct // 100) - 1))
    return values[index]


def _date(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRequest(f"Fecha no válida en {name}: usa AAAA-MM-DD o AAAA-MM-DDTHH:MM") from exc
    # Sin zona se entiende UTC, como la auditoría
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _totals(rows) -> dict:
    keys = ("requests", "success", "blocked", "errors", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "cost_usd", "cache_hits")
    row = rows.one()
    data = {k: int(v or 0) for k, v in zip(keys, row, strict=True) if k != "cost_usd"}
    data["cost_usd"] = plain(row[8] or 0)
    return data


def _measures():
    return (func.count(), func.sum(case((AuditLog.status == AuditStatus.SUCCESS, 1), else_=0)),
            func.sum(case((AuditLog.status == AuditStatus.BLOCKED, 1), else_=0)),
            func.sum(case((AuditLog.status == AuditStatus.ERROR, 1), else_=0)),
            func.sum(AuditLog.input_tokens), func.sum(AuditLog.output_tokens), func.sum(AuditLog.cache_read_tokens),
            func.sum(AuditLog.cache_write_tokens), func.sum(AuditLog.cost_usd),
            func.sum(case((AuditLog.provider_slug == "cache", 1), else_=0)))


@router.get("/overview")
async def overview(request: Request, hours: int = Query(24, ge=1, le=MAX_HOURS), who: Principal = Depends(admin)):
    """Resumen de las últimas `hours` horas: totales, por modelo, por aplicación, por hora y latencias."""
    since = utcnow() - timedelta(hours=hours)
    scope = [AuditLog.organization_id == who.organization_id, AuditLog.created_at >= since]
    async with deps_of(request).sessions() as db:
        totals = _totals(await db.execute(select(*_measures()).where(*scope)))
        by_model = (await db.execute(select(AuditLog.model_key, func.count(), func.sum(AuditLog.cost_usd),
                                            func.sum(AuditLog.input_tokens + AuditLog.output_tokens))
                                     .where(*scope).group_by(AuditLog.model_key)
                                     .order_by(func.count().desc()))).all()
        by_app = (await db.execute(select(AuditLog.application_id, func.count(), func.sum(AuditLog.cost_usd),
                                          func.sum(case((AuditLog.status != AuditStatus.SUCCESS, 1), else_=0)))
                                   .where(*scope).group_by(AuditLog.application_id)
                                   .order_by(func.count().desc()))).all()
        names = dict((await db.execute(select(Application.id, Application.slug).where(
            Application.organization_id == who.organization_id))).all())
        bucket = func.date_format(AuditLog.created_at, "%Y-%m-%dT%H:00:00Z" if hours <= 72 else "%Y-%m-%dT00:00:00Z")
        series = (await db.execute(select(bucket, func.count(),
                                          func.sum(case((AuditLog.status == AuditStatus.ERROR, 1), else_=0)),
                                          func.sum(case((AuditLog.status == AuditStatus.BLOCKED, 1), else_=0)),
                                          func.sum(AuditLog.cost_usd))
                                   .where(*scope).group_by(bucket).order_by(bucket))).all()
        latency = (await db.execute(select(AuditLog.latency_ms, AuditLog.overhead_ms).where(
            *scope, AuditLog.status == AuditStatus.SUCCESS, AuditLog.provider_slug != "cache",
            AuditLog.overhead_ms.is_not(None)).order_by(AuditLog.id.desc()).limit(OVERHEAD_SAMPLE))).all()
    overhead = [r[1] for r in latency]
    total_ms = [r[0] for r in latency if r[0] is not None]
    return {
        "hours": hours, "since": iso(since), "totals": totals,
        "latency_ms": {"p50": _percentile(total_ms, 50), "p95": _percentile(total_ms, 95)},
        "overhead_ms": {"p50": _percentile(overhead, 50), "p95": _percentile(overhead, 95),
                        "p99": _percentile(overhead, 99), "sample": len(overhead), "target_p95": 50},
        "by_model": [{"model": m or "-", "requests": n, "cost_usd": plain(c or 0), "tokens": int(t or 0)}
                     for m, n, c, t in by_model],
        "by_application": [{"application_id": a, "application": names.get(a, str(a)), "requests": n,
                            "cost_usd": plain(c or 0), "failed": int(f or 0)} for a, n, c, f in by_app],
        "series": [{"at": at, "requests": n, "errors": int(e or 0), "blocked": int(b or 0), "cost_usd": plain(c or 0)}
                   for at, n, e, b, c in series],
    }


def _audit_body(row: AuditLog, names: dict) -> dict:
    return {"id": row.id, "request_id": row.request_id, "created_at": iso(row.created_at),
            "completed_at": iso(row.completed_at), "application_id": row.application_id,
            "application": names.get(row.application_id), "team_id": row.team_id, "user_id": row.user_id,
            "end_user_ref": row.end_user_ref, "api_key_prefix": row.api_key_prefix, "purpose": row.purpose,
            "contract": str(row.contract), "stream": row.stream, "requested_model": row.requested_model,
            "model": row.model_key, "provider": row.provider_slug, "deployment_id": row.deployment_id,
            "status": str(row.status), "http_status": row.http_status, "error_type": row.error_type,
            "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens, "cache_write_tokens": row.cache_write_tokens,
            "cost_usd": plain(row.cost_usd), "latency_ms": row.latency_ms, "overhead_ms": row.overhead_ms,
            "attempts": row.attempts, "security_flags": row.security_flags, "prompt_hash": row.prompt_hash,
            "sealed": row.chain_hash is not None}


def _audit_filters(who: Principal, since, until, application_id, status, model, purpose, request_id, end_user):
    where = [AuditLog.organization_id == who.organization_id]
    if since:
        where.append(AuditLog.created_at >= _date(since, "since"))
    if until:
        where.append(AuditLog.created_at < _date(until, "until"))
    if application_id is not None:
        where.append(AuditLog.application_id == application_id)
    if status:
        try:
            where.append(AuditLog.status == AuditStatus(status))
        except ValueError as exc:
            raise InvalidRequest(f"Estado no válido: {status}") from exc
    if model:
        where.append(AuditLog.model_key == model)
    if purpose:
        where.append(AuditLog.purpose == purpose)
    if request_id:
        where.append(AuditLog.request_id == request_id)
    if end_user:
        where.append(AuditLog.end_user_ref == end_user)
    return where


@router.get("/audit")
async def list_audit(request: Request, since: str | None = None, until: str | None = None,
                     application_id: int | None = None, status: str | None = None, model: str | None = None,
                     purpose: str | None = None, request_id: str | None = None, end_user: str | None = None,
                     before_id: int | None = None, limit: int = Query(50, ge=1, le=200),
                     who: Principal = Depends(admin)):
    where = _audit_filters(who, since, until, application_id, status, model, purpose, request_id, end_user)
    if before_id is not None:
        where.append(AuditLog.id < before_id)
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(select(AuditLog).where(*where).order_by(AuditLog.id.desc())
                                 .limit(limit + 1))).scalars().all()
        names = dict((await db.execute(select(Application.id, Application.slug).where(
            Application.organization_id == who.organization_id))).all())
    return page([_audit_body(r, names) for r in rows], limit)


@router.get("/audit/export")
async def export_audit(request: Request, since: str, until: str | None = None, application_id: int | None = None,
                       who: Principal = Depends(admin)):
    """Filas de la auditoría en JSON Lines, con su hash de cadena, para un auditor."""
    where = _audit_filters(who, since, until, application_id, None, None, None, None, None)
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(select(AuditLog).where(*where).order_by(AuditLog.id)
                                 .limit(EXPORT_MAX_ROWS))).scalars().all()

    def lines():
        for row in rows:
            data = json.loads(audit_chain.row_payload(row))
            data["chain_hash"] = row.chain_hash
            data["end_user_ref"] = row.end_user_ref
            yield json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n"

    name = f"auditoria-{(since or '')[:10]}.jsonl"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "no-store"})


@router.get("/audit/chain")
async def chain_state(request: Request, who: Principal = Depends(operator)):
    """Estado del sellado (G9): último eslabón y filas por sellar. Verificar entera: cli audit-verify."""
    async with deps_of(request).sessions() as db:
        state = await db.get(AuditChainState, 1)
        pending = (await db.execute(select(func.count()).select_from(AuditLog).where(
            AuditLog.chain_hash.is_(None)))).scalar()
    return {"last_sealed_id": state.last_id if state else 0, "unsealed_rows": pending,
            "sealed_at": iso(getattr(state, "updated_at", None)) if state else None}


@router.get("/audit/{audit_id}")
async def get_audit(request: Request, audit_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        row = await db.get(AuditLog, audit_id)
        if row is None or row.organization_id != who.organization_id:
            raise NotFound(f"No existe la fila {audit_id} de la auditoría")
        names = dict((await db.execute(select(Application.id, Application.slug).where(
            Application.organization_id == who.organization_id))).all())
    return _audit_body(row, names)


@router.get("/actions")
async def list_actions(request: Request, before_id: int | None = None, limit: int = Query(50, ge=1, le=200),
                       who: Principal = Depends(admin)):
    """Registro de cambios hechos desde el panel. Quien opera el gateway ve también los del catálogo."""
    scope = AdminAction.organization_id == who.organization_id
    if who.platform_operator:
        scope = scope | AdminAction.organization_id.is_(None)
    query = select(AdminAction).where(scope)
    if before_id is not None:
        query = query.where(AdminAction.id < before_id)
    async with deps_of(request).sessions() as db:
        rows = (await db.execute(query.order_by(AdminAction.id.desc()).limit(limit + 1))).scalars().all()
    return page([{"id": r.id, "created_at": iso(r.created_at), "user_id": r.user_id, "action": r.action,
                  "target_type": r.target_type, "target_id": r.target_id, "changes": r.changes,
                  "ip_address": r.ip_address, "organization_id": r.organization_id} for r in rows], limit)
