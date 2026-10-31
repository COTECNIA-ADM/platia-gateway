"""Rutas del contrato de inferencia (cap. 1, ADR-002).

- `POST /v1/messages`: contrato de Anthropic (SDK `anthropic`, PLATIA).
- `POST /v1/chat/completions`: contrato de OpenAI.
- `GET /v1/models`: modelos que puede usar la clave, en el formato de quien pregunta.

El cuerpo se lee a mano y no con un esquema de FastAPI: así se reenvía tal cual, sin
perder campos que el gateway no conoce (ADR-002).
"""
from datetime import UTC

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .errors import AuthenticationFailed, GatewayError, Unavailable, contract_for, error_response
from .models import Contract, Deployment, LlmModel, Provider
from .pipeline import auth
from .pipeline.runner import deps_of, request_id_of, run_inference

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["inferencia"])


@router.post("/messages")
async def messages(request: Request):
    return await run_inference(request, Contract.ANTHROPIC)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await run_inference(request, Contract.OPENAI)


async def _allowed_models(request: Request) -> list[tuple[LlmModel, str]]:
    deps = deps_of(request)
    raw = auth.extract_key(request.headers)
    if not raw:
        raise AuthenticationFailed("Falta la clave API (cabecera Authorization: Bearer o x-api-key)")
    identity = await auth.resolve(deps, raw)
    async with deps.sessions() as db:
        rows = (await db.execute(
            select(LlmModel, Provider.slug)
            .join(Deployment, Deployment.model_id == LlmModel.id)
            .join(Provider, Deployment.provider_id == Provider.id)
            .where(LlmModel.is_active.is_(True), LlmModel.deleted_at.is_(None),
                   Deployment.is_active.is_(True), Deployment.deleted_at.is_(None),
                   Provider.is_active.is_(True), Provider.deleted_at.is_(None))
            .order_by(LlmModel.model_key, Deployment.priority)
        )).all()
    seen: dict[str, tuple[LlmModel, str]] = {}
    for model, provider_slug in rows:
        if identity.allowed_providers is not None and provider_slug not in identity.allowed_providers:
            continue
        if identity.allowed_models is not None and model.model_key not in identity.allowed_models:
            continue
        seen.setdefault(model.model_key, (model, provider_slug))
    return list(seen.values())


@router.get("/models")
async def list_models(request: Request):
    request_id = request_id_of(request)
    contract = contract_for(request)
    try:
        models = await _allowed_models(request)
    except GatewayError as exc:
        return error_response(contract, exc, request_id)
    except SQLAlchemyError:
        log.exception("models.db_error", request_id=request_id)
        return error_response(contract, Unavailable("El gateway no puede consultar su configuración"), request_id)

    headers = {"X-Request-ID": request_id}
    if contract == Contract.ANTHROPIC:
        data = [{"type": "model", "id": m.model_key, "display_name": m.display_name,
                 "created_at": m.created_at.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")}
                for m, _ in models]
        return JSONResponse({"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                             "last_id": data[-1]["id"] if data else None}, headers=headers)
    data = [{"id": m.model_key, "object": "model", "created": int(m.created_at.replace(tzinfo=UTC).timestamp()),
             "owned_by": provider_slug} for m, provider_slug in models]
    return JSONResponse({"object": "list", "data": data}, headers=headers)
