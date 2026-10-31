"""Etapa `policy`: qué puede pedir esta aplicación (cap. 2 y 13).

En G3: propósito permitido, modelo conocido y permitido, y sin streaming. Las cuotas, los
presupuestos y los límites por minuto llegan en G7.
"""
from sqlalchemy import select

from ..errors import InvalidRequest, NotFound, PermissionDenied
from ..models import LlmModel
from .context import Deps, ModelInfo, PipelineContext


async def run(ctx: PipelineContext, deps: Deps) -> None:
    identity = ctx.identity
    if ctx.purpose != "default" and ctx.purpose not in identity.allowed_purposes:
        raise PermissionDenied(f"La aplicación no tiene permitido el propósito '{ctx.purpose}'")
    if ctx.stream:
        raise InvalidRequest("El streaming todavía no está disponible en el gateway: envía stream=false")

    model_key = ctx.requested_model
    async with deps.sessions() as db:
        model = (await db.execute(select(LlmModel).where(
            LlmModel.model_key == model_key, LlmModel.is_active.is_(True), LlmModel.deleted_at.is_(None)
        ))).scalar_one_or_none()
    if model is None:
        raise NotFound(f"Modelo desconocido: {model_key[:128]}")
    if identity.allowed_models is not None and model.model_key not in identity.allowed_models:
        raise PermissionDenied(f"La aplicación no tiene permitido el modelo '{model.model_key}'")
    ctx.model = ModelInfo(id=model.id, key=model.model_key, max_output_tokens=model.max_output_tokens)
