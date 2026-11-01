"""Eventos del gateway: tiempo real por Redis y avisos por webhook (G12, cap. 24, ADR-016).

Un evento es un sobre JSON con id, tipo, fecha, organización y datos. Nunca lleva texto de
prompts ni respuestas, ni claves; la persona final solo aparece como su referencia
(`subject`), igual que en la auditoría.

`emit` lo publica en el canal de Redis que escucha el WebSocket de tiempo real y crea una
entrega por cada webhook suscrito (la envía `app.webhooks` en segundo plano). Los avisos
que se pueden repetir mucho (un bloqueo por petición, un despliegue que se enfría cada
pocos segundos) llevan una clave de deduplicación: como mucho uno cada
`EVENT_DEDUP_SECONDS`. Sin Redis no se puede deduplicar y esos avisos se quedan en el log.

Emitir nunca rompe una petición: un fallo va al log. La idea de publicar y entregar por
separado sigue machinebooks.ai/ES/aigateway/cap-24/block-11.py y block-12.py (MIT).
"""
import json
import uuid
from collections import Counter

import structlog

from .models.base import utcnow
from .pipeline.context import Deps, PipelineContext
from .telemetry import EVENTS, outcome

log = structlog.get_logger(__name__)

CHANNEL = "gw:events"

QUOTA_ALERT = "quota.alert"
QUOTA_EXHAUSTED = "quota.exhausted"
SECURITY_BLOCKED = "security.blocked"
DEPLOYMENT_COOLED = "deployment.cooled"
WEBHOOK_TEST = "webhook.test"
# Solo en tiempo real (uno por petición): no se envía por webhook
REQUEST_FINISHED = "request.finished"

# Los que se pueden suscribir con un webhook
WEBHOOK_EVENTS = (QUOTA_ALERT, QUOTA_EXHAUSTED, SECURITY_BLOCKED, DEPLOYMENT_COOLED)


def envelope(event: str, organization_id: int | None, data: dict) -> dict:
    return {"id": uuid.uuid4().hex, "type": event, "created_at": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "organization_id": organization_id, "data": data}


def dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def publish(deps: Deps, env: dict) -> None:
    """Al canal de tiempo real. Si nadie escucha o Redis no responde, se pierde: es una vista en vivo."""
    if deps.redis is None:
        return
    try:
        await deps.redis.publish(CHANNEL, dumps(env))
    except Exception as exc:  # noqa: BLE001 - sin Redis no hay tiempo real, la petición sigue
        log.warning("event.publish_failed", event_type=env["type"], error_type=type(exc).__name__)


async def emit(deps: Deps, event: str, *, organization_id: int | None, data: dict, dedupe: str | None = None) -> None:
    from . import webhooks  # evita el ciclo: webhooks usa el catálogo de aquí

    try:
        if dedupe is not None:
            if deps.redis is None:
                log.info("event.not_deduplicated", event_type=event, organization_id=organization_id)
                return
            first = await deps.redis.set(f"gw:evt:{event}:{dedupe}", 1, ex=deps.settings.event_dedup_seconds,
                                         nx=True)
            if not first:
                return
    except Exception as exc:  # noqa: BLE001
        log.warning("event.dedupe_failed", event_type=event, error_type=type(exc).__name__)
        return
    env = envelope(event, organization_id, data)
    EVENTS.labels(event).inc()
    log.info("event.emitted", event_type=event, event_id=env["id"], organization_id=organization_id)
    await publish(deps, env)
    try:
        await webhooks.enqueue(deps, env)
    except Exception as exc:  # noqa: BLE001 - un aviso perdido va al log, la petición sigue
        log.error("event.enqueue_failed", event_type=event, event_id=env["id"], error_type=type(exc).__name__)


async def request_finished(ctx: PipelineContext, deps: Deps) -> None:
    """Resumen de una petición para el panel en vivo: sin persona final ni texto."""
    identity = ctx.identity
    data = {
        "request_id": ctx.request_id, "application": identity.application_slug, "purpose": ctx.purpose,
        "contract": str(ctx.contract), "stream": ctx.stream,
        "model": ctx.model.key if ctx.model else None,
        "provider": "cache" if ctx.cache_hit else (ctx.target.provider_slug if ctx.target else None),
        "outcome": outcome(ctx), "http_status": ctx.error.status if ctx.error else 200,
        "error_type": ctx.error.kind if ctx.error else None,
        "latency_ms": ctx.elapsed_ms(), "overhead_ms": ctx.overhead_ms, "attempts": ctx.attempts,
        "input_tokens": ctx.usage.input, "output_tokens": ctx.usage.output, "cost_usd": str(ctx.cost_usd),
        "cache": ctx.cache_state,
    }
    await publish(deps, envelope(REQUEST_FINISHED, identity.organization_id, data))


async def security_blocked(ctx: PipelineContext, deps: Deps) -> None:
    identity = ctx.identity
    blocks = [f for f in ctx.security_flags if f.get("action") == "block"]
    flag = blocks[-1] if blocks else {}
    direction, check = flag.get("direction", "-"), flag.get("check", "-")
    data = {"application": identity.application_slug, "purpose": ctx.purpose, "direction": direction,
            "check": check, "kinds": dict(Counter(flag.get("kinds") or {})), "request_id": ctx.request_id,
            "window_seconds": deps.settings.event_dedup_seconds}
    await emit(deps, SECURITY_BLOCKED, organization_id=identity.organization_id, data=data,
               dedupe=f"{identity.organization_id}:{identity.application_id}:{direction}:{check}")
