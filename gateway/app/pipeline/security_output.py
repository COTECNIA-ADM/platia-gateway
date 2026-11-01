"""Etapa `security_output`: secretos y datos personales en la respuesta sin streaming (cap. 20, ADR-012).

Tacha, anota o retiene (403) la respuesta según la política de salida. En streaming hace lo
mismo `app.streaming` trozo a trozo, con `content.TextGuard`.
"""
from ..content import response_slots
from ..content_policy import scan
from ..models import ContentCheck, Direction
from .context import Deps, PipelineContext


async def run(ctx: PipelineContext, deps: Deps) -> None:
    if ctx.response_body:
        scan(ctx, Direction.OUTPUT, [ContentCheck.SECRET, ContentCheck.PII], list(response_slots(ctx.response_body)))
