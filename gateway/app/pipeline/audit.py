"""Etapa `audit`: una fila por petición, con quién, qué, resultado, tokens y coste (cap. 21).

Se escribe en dos pasos (ADR-005):

1. **Apertura**, antes de llamar al proveedor: fila `pending`. Si no se puede escribir, la
   petición no sale (fallar cerrado: no hay gasto sin registro).
2. **Cierre**: tokens, coste, latencia y estado. Si falla, la respuesta ya está pagada y se
   entrega igual; queda en el log como `audit.close_failed` (la conciliación llega en G9).
   En streaming se cierra al terminar el envío, también si el cliente se desconecta.

Las peticiones rechazadas después de identificar la aplicación también quedan registradas
(`blocked` o `error`). Las que no llegan a identificarse solo van al log. Nunca se guarda
el texto de la conversación.
"""
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from ..audit_chain import HASH_VERSION, keyed_digest, prompt_digest
from ..contracts import Usage
from ..errors import PermissionDenied, QuotaExceeded, RateLimited, Unavailable
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
        # Huellas con la sal de la organización (G9): sin el texto ni el valor
        "end_user_digest": keyed_digest(identity.audit_salt, ctx.end_user_ref) if ctx.end_user_ref else None,
        "prompt_hash": prompt_digest(identity.audit_salt, ctx.body) if ctx.body else None,
        "hash_version": HASH_VERSION,
        "requested_model": (ctx.requested_model or "")[:128] or None,
        "model_key": ctx.model.key if ctx.model else None,
        "deployment_id": ctx.target.deployment_id if ctx.target else None,
        "provider_slug": ctx.target.provider_slug if ctx.target else None,
        # Lo que encontró la seguridad de contenido (G8): categoría, acción y recuento, sin el texto
        "security_flags": list(ctx.security_flags) or None,
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


async def close(ctx: PipelineContext, deps: Deps) -> None:
    """Cierra la fila (o la crea, si el corte llegó antes de abrirla) con el resultado, los tokens y el coste.

    Sin identidad no hay organización a la que atribuir la petición: solo queda en el log.
    Un fallo aquí no cambia la respuesta (ya está entregada o pagada): va al log como
    `audit.close_failed` para conciliarlo (G9).
    """
    if ctx.identity is None:
        return
    error, usage = ctx.error, ctx.usage
    if error is None:
        status = AuditStatus.SUCCESS
    else:
        blocked = isinstance(error, PermissionDenied | QuotaExceeded | RateLimited)
        status = AuditStatus.BLOCKED if blocked else AuditStatus.ERROR
    values = {"status": status, "http_status": error.status if error else 200,
              "error_type": error.kind if error else None, "completed_at": utcnow(), "latency_ms": ctx.elapsed_ms(),
              "input_tokens": usage.input, "output_tokens": usage.output, "cache_read_tokens": usage.cache_read,
              "cache_write_tokens": usage.cache_write, "attempts": ctx.attempts,
              # Con reserva, el destino final puede no ser el de la apertura (G6)
              "model_key": ctx.model.key if ctx.model else None,
              "deployment_id": ctx.target.deployment_id if ctx.target else None,
              "provider_slug": ctx.target.provider_slug if ctx.target else None,
              "security_flags": list(ctx.security_flags) or None}
    try:
        async with deps.sessions() as db:
            if ctx.model is not None and (usage.input or usage.output or usage.cache_read or usage.cache_write):
                price = (await db.execute(
                    select(ModelPrice).where(ModelPrice.model_id == ctx.model.id,
                                             ModelPrice.effective_from <= utcnow())
                    .order_by(ModelPrice.effective_from.desc()).limit(1)
                )).scalar_one_or_none()
                ctx.cost_usd = compute_cost(usage, price)
            values["cost_usd"] = ctx.cost_usd
            if ctx.audit_id is None:
                db.add(AuditLog(**{**_who(ctx), **values}))
            else:
                # Solo si sigue pendiente: si la conciliación (G9) ya la cerró, se respeta
                result = await db.execute(update(AuditLog).where(
                    AuditLog.id == ctx.audit_id, AuditLog.status == AuditStatus.PENDING).values(**values))
                if not result.rowcount:
                    log.warning("audit.close_late", request_id=ctx.request_id, audit_id=ctx.audit_id,
                                input_tokens=usage.input, output_tokens=usage.output)
            await db.commit()
    except SQLAlchemyError as exc:
        log.error("audit.close_failed", request_id=ctx.request_id, audit_id=ctx.audit_id,
                  error_type=type(exc).__name__, input_tokens=usage.input, output_tokens=usage.output)
