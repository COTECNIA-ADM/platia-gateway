"""Etapa `execute`: llamada al proveedor, con reserva, y respuesta en el contrato del cliente (cap. 4, 6 y 7).

Prueba los destinos que ordenó `route`. Si uno falla por algo suyo (saturado, caído, sin
respuesta, mal configurado: `GatewayError.fallback`), lo apunta para el enfriamiento y pasa
al siguiente. Un error de la petición (400, 413) no cambia de destino: otro la rechazaría
igual. No se empieza otro intento pasado `ROUTE_RETRY_WINDOW_SECONDS`.

En streaming, la reserva solo es posible antes de responder: se abre la respuesta del
proveedor, se comprueba su código y, si falla, se prueba el siguiente. Una vez enviado el
primer evento, un corte ya no se puede reintentar sin duplicar texto. Los eventos los
reenvía `app.streaming`.
"""
from collections.abc import Awaitable, Callable

import structlog

from .. import cooldown
from ..contracts import anthropic_to_openai_response, native_contract, openai_to_anthropic_response, usage_of
from ..errors import GatewayError
from ..models import Contract
from ..providers import open_stream, send
from .context import Deps, PipelineContext

log = structlog.get_logger(__name__)


async def _with_fallback(ctx: PipelineContext, deps: Deps, call: Callable[[], Awaitable]):
    last_error: GatewayError | None = None
    window_ms = deps.settings.route_retry_window_seconds * 1000
    for index, candidate in enumerate(ctx.candidates):
        if index and ctx.elapsed_ms() > window_ms:
            log.warning("route.retry_window_exhausted", request_id=ctx.request_id, attempts=index)
            break
        ctx.model, ctx.target = candidate.model, candidate.target
        ctx.upstream_body, ctx.hide_usage_chunk = candidate.body, candidate.hide_usage_chunk
        ctx.attempts = index + 1
        try:
            result = await call()
        except GatewayError as exc:
            if not exc.fallback:
                raise
            await cooldown.record_failure(deps.redis, deps.settings, candidate.target.deployment_id, exc)
            last_error = exc
            if index + 1 < len(ctx.candidates):
                log.warning("route.fallback", request_id=ctx.request_id, model=candidate.model.key,
                            deployment_id=candidate.target.deployment_id, provider=candidate.target.provider_slug,
                            error_type=exc.kind, status=exc.status)
            continue
        await cooldown.record_success(deps.redis, candidate.target.deployment_id)
        return result
    raise last_error


async def run(ctx: PipelineContext, deps: Deps) -> None:
    data = await _with_fallback(ctx, deps, lambda: send(deps.http, ctx.target, ctx.upstream_body, ctx.headers))
    native = native_contract(ctx.target.kind)
    ctx.usage = usage_of(data, native)
    if native == ctx.contract:
        ctx.response_body = data
    elif ctx.contract == Contract.OPENAI:
        ctx.response_body = anthropic_to_openai_response(data, model=ctx.model.key, request_id=ctx.request_id)
    else:
        ctx.response_body = openai_to_anthropic_response(data, model=ctx.model.key, request_id=ctx.request_id)


async def open_upstream_stream(ctx: PipelineContext, deps: Deps):
    return await _with_fallback(ctx, deps,
                                lambda: open_stream(deps.http, ctx.target, ctx.upstream_body, ctx.headers))
