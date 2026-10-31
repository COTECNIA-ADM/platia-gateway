"""Etapa `audit`: una fila por petición, con quién, qué, resultado, tokens y coste (cap. 21).

Se escribe en dos pasos (ADR-005):

1. **Apertura**, antes de llamar al proveedor: fila `pending`. Si no se puede escribir, la
   petición no sale (fallar cerrado: no hay gasto sin registro).
2. **Cierre**: tokens, coste, latencia y estado. Si falla, la respuesta ya está pagada y se
   entrega igual; queda en el log como `audit.close_failed` (la conciliación llega en G9).

Las peticiones rechazadas después de identificar la aplicación también quedan registradas
(`blocked` o `error`). Las que no llegan a identificarse solo van al log. Nunca se guarda
el texto de la conversación.
"""
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from ..contracts import Usage
from ..errors import PermissionDenied, Unavailable
from ..models import AuditLog, AuditStatus, ModelPrice
from ..models.base import utcnow
from .context import Deps, PipelineContext

log = structlog.get_logger(__name__)
_MTOK = Decimal(1_000_000)


def compute_cost(usage: Usage, price: ModelPrice | None) -> Decimal:
    """Coste en USD. Sin precio de caché propio, esos tokens se cobran como entrada normal."""
    if price is None:
        return Decimal("0")
    cache_read = price.cache_read_per_mtok if price.cache_read_per_mtok is not None else price.input_per_mtok
    cache_write = price.cache_write_per_mtok if price.cache_write_per_mtok is not None else price.input_per_mtok
    total = (usage.input * price.input_per_mtok + usage.output * price.output_per_mtok
             + usage.cache_read * cache_read + usage.cache_write * cache_write)
    return (total / _MTOK).quantize(Decimal("0.00000001"))


def _who(ctx: PipelineContext) -> dict:
    identity = ctx.identity
    return {
        "request_id": ctx.request_id, "contract": ctx.contract, "stream": ctx.stream,
        "organization_id": identity.organization_id, "team_id": identity.team_id,
        "application_id": identity.application_id, "api_key_prefix": identity.api_key_prefix,
        "end_user_ref": ctx.end_user_ref, "purpose": ctx.purpose,
        "requested_model": (ctx.requested_model or "")[:128] or None,
        "model_key": ctx.model.key if ctx.model else None,
        "deployment_id": ctx.target.deployment_id if ctx.target else None,
        "provider_slug": ctx.target.provider_slug if ctx.target else None,
    }


async def open_record(ctx: PipelineContext, deps: Deps) -> None:
    try:
        async with deps.sessions() as db:
            row = AuditLog(**_who(ctx), status=AuditStatus.PENDING)
            db.add(row)
            await db.commit()
            ctx.audit_id = row.id
    except SQLAlchemyError as exc:
        log.error("audit.open_failed", request_id=ctx.request_id, error_type=type(exc).__name__)
        raise Unavailable("No se puede registrar la petición; no se ha enviado al proveedor") from exc


async def close_success(ctx: PipelineContext, deps: Deps) -> None:
    usage = ctx.usage
    try:
        async with deps.sessions() as db:
            price = (await db.execute(
                select(ModelPrice).where(ModelPrice.model_id == ctx.model.id, ModelPrice.effective_from <= utcnow())
                .order_by(ModelPrice.effective_from.desc()).limit(1)
            )).scalar_one_or_none()
            ctx.cost_usd = compute_cost(usage, price)
            await db.execute(update(AuditLog).where(AuditLog.id == ctx.audit_id).values(
                status=AuditStatus.SUCCESS, http_status=200, completed_at=utcnow(), latency_ms=ctx.elapsed_ms(),
                input_tokens=usage.input, output_tokens=usage.output, cache_read_tokens=usage.cache_read,
                cache_write_tokens=usage.cache_write, cost_usd=ctx.cost_usd))
            await db.commit()
    except SQLAlchemyError as exc:
        log.error("audit.close_failed", request_id=ctx.request_id, audit_id=ctx.audit_id,
                  error_type=type(exc).__name__, input_tokens=usage.input, output_tokens=usage.output)
    ctx.finished = True


async def close_failure(ctx: PipelineContext, deps: Deps) -> None:
    """Registra un corte o un error. Sin identidad no hay organización a la que atribuirlo: solo log."""
    error = ctx.error
    if ctx.identity is None:
        return
    values = {"status": AuditStatus.BLOCKED if isinstance(error, PermissionDenied) else AuditStatus.ERROR,
              "http_status": error.status, "error_type": error.kind, "completed_at": utcnow(),
              "latency_ms": ctx.elapsed_ms()}
    try:
        async with deps.sessions() as db:
            if ctx.audit_id is None:
                db.add(AuditLog(**_who(ctx), **values))
            else:
                await db.execute(update(AuditLog).where(AuditLog.id == ctx.audit_id).values(**values))
            await db.commit()
    except SQLAlchemyError as exc:
        log.error("audit.close_failed", request_id=ctx.request_id, audit_id=ctx.audit_id,
                  error_type=type(exc).__name__)
