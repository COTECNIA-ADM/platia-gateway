"""Orden de las etapas y forma de la respuesta (cap. 3 y 4)."""
import json
import re
import uuid

import anyio
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from ..contracts import check_basics
from ..errors import GatewayError, InternalError, InvalidRequest, RequestTooLarge, Unavailable, error_response
from ..models import Contract
from ..streaming import streaming_response
from ..content_policy import header as content_header
from . import audit, auth, cache, execute, filter, policy, quota, route, security_input, security_output
from .context import Deps, PipelineContext

log = structlog.get_logger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def request_id_of(request: Request) -> str:
    """El de Nginx (o el del cliente) si tiene buena forma; si no, uno nuevo."""
    candidate = request.headers.get("x-request-id", "")
    return candidate if _REQUEST_ID.match(candidate) else uuid.uuid4().hex


async def read_json(request: Request, limit: int) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise RequestTooLarge("El cuerpo de la petición es demasiado grande")
    raw = await request.body()
    if len(raw) > limit:
        raise RequestTooLarge("El cuerpo de la petición es demasiado grande")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise InvalidRequest("El cuerpo de la petición debe ser JSON") from exc
    if not isinstance(body, dict):
        raise InvalidRequest("El cuerpo de la petición debe ser un objeto JSON")
    return body


def deps_of(request: Request) -> Deps:
    return request.app.state.deps


async def run_inference(request: Request, contract: Contract):
    deps = deps_of(request)
    ctx = PipelineContext(request_id=request_id_of(request), contract=contract, headers=request.headers)
    upstream = None
    try:
        await auth.run(ctx, deps)
        ctx.body = await read_json(request, deps.settings.max_body_bytes)
        ctx.requested_model = check_basics(ctx.body)
        ctx.stream = bool(ctx.body.get("stream", False))
        await security_input.run(ctx, deps)
        await filter.run(ctx, deps)
        await policy.run(ctx, deps)
        await cache.run(ctx, deps)
        if ctx.cache_hit:
            # Respuesta guardada (G11): ni cuota ni proveedor; sí filtro de salida y auditoría
            await audit.open_record(ctx, deps)
            await security_output.run(ctx, deps)
            await audit.close(ctx, deps)
            ctx.finished = True
            return _response(ctx, contract)
        await quota.run(ctx, deps)
        await route.run(ctx, deps)
        await audit.open_record(ctx, deps)
        if ctx.stream:
            # A partir de aquí la respuesta (y el cierre de la auditoría) es del generador
            upstream = await execute.open_upstream_stream(ctx, deps)
        else:
            await execute.run(ctx, deps)
            await cache.store(ctx, deps)
            await security_output.run(ctx, deps)
            await audit.close(ctx, deps)
            ctx.finished = True
    except GatewayError as exc:
        ctx.error = exc
    except SQLAlchemyError:
        log.exception("pipeline.db_error", request_id=ctx.request_id)
        ctx.error = Unavailable("El gateway no puede consultar su configuración en este momento")
    except Exception:
        log.exception("pipeline.unexpected", request_id=ctx.request_id)
        ctx.error = InternalError("Error interno del gateway")
    finally:
        if upstream is None:
            with anyio.CancelScope(shield=True):
                if not ctx.finished:
                    if ctx.error is None:
                        # Cancelada a mitad (cliente desconectado o apagado): también se registra
                        ctx.error = GatewayError("Petición cancelada", status=499, kind="cancelled")
                    await audit.close(ctx, deps)
                # Con streaming, los contadores los ajusta el generador al terminar
                await quota.settle(ctx, deps)

    if ctx.error is not None:
        log.info("request.failed", request_id=ctx.request_id, contract=contract, status=ctx.error.status,
                 error_type=ctx.error.kind, application=ctx.identity.application_slug if ctx.identity else None)
        ctx.error.headers = {**ctx.error.headers, **content_header(ctx)}
        return error_response(contract, ctx.error, ctx.request_id)

    if upstream is not None:
        return streaming_response(ctx, deps, upstream)

    return _response(ctx, contract)


def _response(ctx: PipelineContext, contract: Contract) -> JSONResponse:
    log.info("request.completed", request_id=ctx.request_id, contract=contract,
             application=ctx.identity.application_slug, purpose=ctx.purpose, model=ctx.model.key,
             provider="cache" if ctx.cache_hit else ctx.target.provider_slug, attempts=ctx.attempts,
             input_tokens=ctx.usage.input, output_tokens=ctx.usage.output, cost_usd=str(ctx.cost_usd),
             latency_ms=ctx.elapsed_ms(), cache=ctx.cache_state)
    headers = {
        "X-Request-ID": ctx.request_id,
        "X-Platia-Model-Used": ctx.model.key,
        "X-Platia-Attempts": str(ctx.attempts),
        "X-Platia-Cost-Usd": f"{ctx.cost_usd:.8f}",
        "X-Platia-Latency-Ms": str(ctx.elapsed_ms()),
        **(ctx.quota.headers if ctx.quota else {}),
        **content_header(ctx),
    }
    if ctx.cache_state != "off":
        headers["X-Platia-Cache"] = ctx.cache_state
    return JSONResponse(ctx.response_body, headers=headers)
