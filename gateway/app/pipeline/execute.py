"""Etapa `execute`: llamada al proveedor y respuesta en el contrato del cliente (cap. 4 y 7).

En streaming solo abre la respuesta y comprueba el código; los eventos los reenvía
`app.streaming`.
"""
from ..contracts import anthropic_to_openai_response, native_contract, openai_to_anthropic_response, usage_of
from ..models import Contract
from ..providers import open_stream, send
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


async def open_upstream_stream(ctx: PipelineContext, deps: Deps):
    return await open_stream(deps.http, ctx.target, ctx.upstream_body, ctx.headers)
