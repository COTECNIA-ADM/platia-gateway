"""Etapa `execute`: llamada al proveedor y respuesta en el contrato del cliente (cap. 7)."""
from ..contracts import anthropic_to_openai_response, native_contract, openai_to_anthropic_response, usage_of
from ..models import Contract
from ..providers import send
from .context import Deps, PipelineContext


async def run(ctx: PipelineContext, deps: Deps) -> None:
    data = await send(deps.http, ctx.target, ctx.upstream_body, ctx.headers)
    native = native_contract(ctx.target.kind)
    ctx.usage = usage_of(data, native)
    if native == ctx.contract:
        ctx.response_body = data
    elif ctx.contract == Contract.OPENAI:
        ctx.response_body = anthropic_to_openai_response(data, model=ctx.model.key, request_id=ctx.request_id)
    else:
        ctx.response_body = openai_to_anthropic_response(data, model=ctx.model.key, request_id=ctx.request_id)
