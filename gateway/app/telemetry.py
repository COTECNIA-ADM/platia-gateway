"""Métricas de Prometheus y cierre de la telemetría de cada petición (G12, cap. 24, ADR-016).

`/metrics` publica contadores e histogramas para un Prometheus de la red interna. Nginx no
la publica y además exige `Authorization: Bearer <METRICS_TOKEN>`; sin token configurado
no existe (404): fallar cerrado.

Etiquetas de cardinalidad baja y conocida: aplicación, modelo, proveedor, contrato y
resultado. Nunca la persona final, la clave ni el id de la petición (eso está en la
auditoría y en los logs).

Con varios procesos de uvicorn, cada uno escribe sus valores en `PROMETHEUS_MULTIPROC_DIR`
y `/metrics` los suma (modo multiproceso de prometheus_client). Sin esa variable (tests, un
solo proceso) se usa el registro normal.

`finish(ctx, deps)` se llama una sola vez al terminar cada petición de inferencia, también
en streaming y en errores: registra las métricas y lanza los eventos que tocan (resumen en
tiempo real y aviso de bloqueo de seguridad). Un fallo aquí nunca cambia la respuesta.

Los histogramas y las etiquetas siguen machinebooks.ai/ES/aigateway/cap-24/block-02.py a
block-04.py (MIT).
"""
import hmac
import os

import structlog
from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, Counter, Histogram, generate_latest

from .errors import ContentBlocked, PermissionDenied, QuotaExceeded, RateLimited
from .pipeline.context import Deps, PipelineContext

log = structlog.get_logger(__name__)

PREFIX = "platia_gateway"

REQUESTS = Counter(f"{PREFIX}_requests", "Peticiones de inferencia terminadas",
                   ["contract", "application", "model", "provider", "outcome"])
DURATION = Histogram(f"{PREFIX}_request_duration_seconds", "Duración total de la petición, proveedor incluido",
                     ["contract", "stream"], buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300))
# RNF-06: tiempo del gateway sin el proveedor; objetivo p95 < 50 ms
OVERHEAD = Histogram(f"{PREFIX}_overhead_seconds", "Tiempo propio del gateway, sin contar el proveedor",
                     ["contract", "stream"],
                     buckets=(0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.25, 0.5, 1))
TOKENS = Counter(f"{PREFIX}_tokens", "Tokens consumidos", ["model", "kind"])
COST = Counter(f"{PREFIX}_cost_usd", "Coste en USD", ["application", "model"])
UPSTREAM_ERRORS = Counter(f"{PREFIX}_upstream_errors", "Fallos de un proveedor (cada intento)",
                          ["provider", "error_type"])
SECURITY_FLAGS = Counter(f"{PREFIX}_security_flags", "Hallazgos de la seguridad de contenido",
                         ["direction", "check", "action"])
CACHE = Counter(f"{PREFIX}_cache_lookups", "Consultas a la caché exacta", ["result"])
QUOTA_REJECTIONS = Counter(f"{PREFIX}_quota_rejections", "Peticiones rechazadas por cuota o límite de ritmo",
                           ["scope", "metric", "period"])
EVENTS = Counter(f"{PREFIX}_events", "Eventos emitidos (avisos)", ["event"])
WEBHOOK_DELIVERIES = Counter(f"{PREFIX}_webhook_deliveries", "Intentos de entrega de webhooks", ["result"])

router = APIRouter(include_in_schema=False)


def outcome(ctx: PipelineContext) -> str:
    error = ctx.error
    if error is None:
        return "success"
    if ctx.identity is None:
        return "unauthenticated"
    if error.status == 499:
        return "cancelled"
    if isinstance(error, PermissionDenied | QuotaExceeded | RateLimited):
        return "blocked"
    return "error"


def _observe(ctx: PipelineContext) -> None:
    identity, usage = ctx.identity, ctx.usage
    application = identity.application_slug if identity else "-"
    model = ctx.model.key if ctx.model else "-"
    provider = "cache" if ctx.cache_hit else (ctx.target.provider_slug if ctx.target else "-")
    contract, stream = str(ctx.contract), "true" if ctx.stream else "false"
    REQUESTS.labels(contract, application, model, provider, outcome(ctx)).inc()
    DURATION.labels(contract, stream).observe(ctx.elapsed_ms() / 1000)
    # Solo si el proveedor respondió: el tiempo de un rechazo no es sobrecoste de una llamada
    if ctx.overhead_seconds is not None and ctx.error is None and not ctx.cache_hit:
        OVERHEAD.labels(contract, stream).observe(ctx.overhead_seconds)
    for kind, value in (("input", usage.input), ("output", usage.output), ("cache_read", usage.cache_read),
                        ("cache_write", usage.cache_write)):
        if value:
            TOKENS.labels(model, kind).inc(value)
    if ctx.cost_usd:
        COST.labels(application, model).inc(float(ctx.cost_usd))
    for flag in ctx.security_flags:
        SECURITY_FLAGS.labels(flag.get("direction", "-"), flag.get("check", "-"), flag.get("action", "-")).inc()
    if ctx.cache_state in ("hit", "miss"):
        CACHE.labels(ctx.cache_state).inc()


async def finish(ctx: PipelineContext, deps: Deps) -> None:
    """Métricas y eventos de una petición terminada. Solo cuenta una vez y nunca lanza."""
    if ctx.observed:
        return
    ctx.observed = True
    try:
        _observe(ctx)
    except Exception as exc:  # noqa: BLE001 - la telemetría no rompe una respuesta
        log.warning("telemetry.observe_failed", request_id=ctx.request_id, error_type=type(exc).__name__)
    if ctx.identity is None:
        return
    from . import events  # evita el ciclo: events usa los contadores de aquí

    await events.request_finished(ctx, deps)
    if isinstance(ctx.error, ContentBlocked):
        await events.security_blocked(ctx, deps)


def _registry():
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return REGISTRY


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    expected = request.app.state.settings.metrics_token
    if not expected:
        return Response(status_code=404)
    given = request.headers.get("authorization", "")
    token = given[7:].strip() if given[:7].lower() == "bearer " else ""
    if not hmac.compare_digest(token.encode(), expected.encode()):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return Response(generate_latest(_registry()), media_type=CONTENT_TYPE_LATEST)
