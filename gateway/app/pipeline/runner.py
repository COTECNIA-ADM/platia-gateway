"""Orden de las etapas y forma de la respuesta (cap. 3)."""
import json
import re
import uuid

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from ..contracts import check_basics
from ..errors import GatewayError, InternalError, InvalidRequest, RequestTooLarge, Unavailable, error_response
from ..models import Contract
from . import audit, auth, execute, policy, route
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
    try:
        await auth.run(ctx, deps)
        ctx.body = await read_json(request, deps.settings.max_body_bytes)
        ctx.requested_model = check_basics(ctx.body)
        ctx.stream = bool(ctx.body.get("stream", False))
        await policy.run(ctx, deps)
        await route.run(ctx, deps)
        await audit.open_record(ctx, deps)
        await execute.run(ctx, deps)
        await audit.close_success(ctx, deps)
    except GatewayError as exc:
        ctx.error = exc
    except SQLAlchemyError:
        log.exception("pipeline.db_error", request_id=ctx.request_id)
        ctx.error = Unavailable("El gateway no puede consultar su configuración en este momento")
    except Exception:
        log.exception("pipeline.unexpected", request_id=ctx.request_id)
        ctx.error = InternalError("Error interno del gateway")
    finally:
        if not ctx.finished and ctx.error is None:
            # Cancelada a mitad (cliente desconectado o apagado): también se registra
            ctx.error = GatewayError("Petición cancelada", status=499, kind="cancelled")
        if ctx.error is not None:
            await audit.close_failure(ctx, deps)

    if ctx.error is not None:
        log.info("request.failed", request_id=ctx.request_id, contract=contract, status=ctx.error.status,
                 error_type=ctx.error.kind, application=ctx.identity.application_slug if ctx.identity else None)
        return error_response(contract, ctx.error, ctx.request_id)

    log.info("request.completed", request_id=ctx.request_id, contract=contract,
             application=ctx.identity.application_slug, purpose=ctx.purpose, model=ctx.model.key,
             provider=ctx.target.provider_slug, input_tokens=ctx.usage.input, output_tokens=ctx.usage.output,
             cost_usd=str(ctx.cost_usd), latency_ms=ctx.elapsed_ms())
    return JSONResponse(ctx.response_body, headers={
        "X-Request-ID": ctx.request_id,
        "X-Platia-Model-Used": ctx.model.key,
        "X-Platia-Cost-Usd": f"{ctx.cost_usd:.8f}",
        "X-Platia-Latency-Ms": str(ctx.elapsed_ms()),
    })
