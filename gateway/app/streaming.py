"""Respuestas en streaming (SSE) en los dos contratos (cap. 4, ADR-008).

El proveedor envía eventos SSE; el gateway los reenvía al cliente a medida que llegan y,
de paso, lee el uso de tokens para la auditoría:

- **Misma familia**: cada evento sale tal cual, byte a byte (razonamiento, firmas,
  herramientas y eventos nuevos incluidos). Solo se leen los eventos con el uso.
- **Familias distintas**: solo texto, como en las respuestas normales (ADR-007). Los
  eventos se reconstruyen en el contrato del cliente.

La auditoría se cierra en el `finally` del generador: también cuando el proveedor corta,
cuando se agota el tiempo y cuando el cliente se desconecta a mitad.
"""
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import perf_counter

import anyio
import httpx
import structlog
from fastapi.responses import StreamingResponse

from .contracts import (STOP_TO_ANTHROPIC, STOP_TO_OPENAI, Usage, merge_anthropic_usage, native_contract,
                        usage_from_openai)
from .errors import GatewayError, UpstreamFailed, UpstreamTimeout
from .models import Contract
from .pipeline import audit, quota
from .pipeline.context import Deps, PipelineContext
from .providers import sanitize

log = structlog.get_logger(__name__)

DONE = "[DONE]"


# --- Lectura de SSE

@dataclass(frozen=True)
class Event:
    name: str | None  # línea "event:" (Anthropic); None en OpenAI
    data: str  # líneas "data:" unidas
    raw: str  # el bloque tal cual, para reenviarlo sin tocarlo

    def json(self) -> dict | None:
        try:
            value = json.loads(self.data)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


def _block(lines: list[str]) -> Event:
    name, data = None, []
    for line in lines:
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
    return Event(name=name, data="\n".join(data), raw="\n".join(lines) + "\n\n")


async def sse_events(response: httpx.Response) -> AsyncIterator[Event]:
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line:
            lines.append(line)
        elif lines:
            yield _block(lines)
            lines = []
    if lines:
        yield _block(lines)


def anthropic_event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def openai_event(payload: dict | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n"


def _upstream_error(payload: dict | None) -> GatewayError:
    error = (payload or {}).get("error") or {}
    message = sanitize(error.get("message") or "") if isinstance(error, dict) else ""
    kind = error.get("type") if isinstance(error, dict) and isinstance(error.get("type"), str) else "api_error"
    return GatewayError(f"El proveedor cortó la respuesta: {message or 'error'}", status=502, kind=kind[:64])


# --- Traductores: un evento del proveedor → eventos para el cliente

class Relay:
    """Base: lleva el uso, si la respuesta terminó y el error del proveedor, si lo hubo."""
    client_contract: Contract

    def __init__(self):
        self.usage = Usage()
        self.done = False
        self.error: GatewayError | None = None

    def feed(self, event: Event) -> list[str]:  # pragma: no cover - lo implementa cada traductor
        raise NotImplementedError

    def error_event(self, error: GatewayError) -> list[str]:
        if self.client_contract == Contract.ANTHROPIC:
            return [anthropic_event("error", {"type": "error", "error": {"type": error.kind,
                                                                          "message": error.message}})]
        return [openai_event({"error": {"message": error.message, "type": error.kind, "param": None,
                                        "code": None}})]


class AnthropicPassthrough(Relay):
    client_contract = Contract.ANTHROPIC

    def feed(self, event: Event) -> list[str]:
        if event.name == "message_start":
            merge_anthropic_usage(self.usage, ((event.json() or {}).get("message") or {}).get("usage"))
        elif event.name == "message_delta":
            merge_anthropic_usage(self.usage, (event.json() or {}).get("usage"))
        elif event.name == "message_stop":
            self.done = True
        elif event.name == "error":
            self.error, self.done = _upstream_error(event.json()), True
        return [event.raw]


class OpenAIPassthrough(Relay):
    client_contract = Contract.OPENAI

    def __init__(self, hide_usage_chunk: bool):
        super().__init__()
        # El gateway pidió el uso aunque el cliente no lo hiciera: ese fragmento extra no se reenvía
        self.hide_usage_chunk = hide_usage_chunk

    def feed(self, event: Event) -> list[str]:
        if event.data == DONE:
            self.done = True
            return [event.raw]
        chunk = event.json() or {}
        if chunk.get("error"):
            self.error, self.done = _upstream_error(chunk), True
            return [event.raw]
        if chunk.get("usage"):
            self.usage = usage_from_openai(chunk)
            if self.hide_usage_chunk and not chunk.get("choices"):
                return []
        return [event.raw]


class AnthropicToOpenAI(Relay):
    """Proveedor Anthropic, cliente con el contrato de OpenAI (solo texto)."""
    client_contract = Contract.OPENAI

    def __init__(self, model: str, request_id: str, include_usage: bool):
        super().__init__()
        self.base = {"id": f"chatcmpl-{request_id}", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model}
        self.include_usage = include_usage

    def _chunk(self, delta: dict, finish_reason: str | None = None) -> str:
        return openai_event({**self.base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})

    def feed(self, event: Event) -> list[str]:
        payload = event.json() or {}
        if event.name == "message_start":
            merge_anthropic_usage(self.usage, (payload.get("message") or {}).get("usage"))
            return [self._chunk({"role": "assistant", "content": ""})]
        if event.name == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [self._chunk({"content": delta.get("text", "")})]
            return []
        if event.name == "message_delta":
            merge_anthropic_usage(self.usage, payload.get("usage"))
            reason = (payload.get("delta") or {}).get("stop_reason")
            return [self._chunk({}, STOP_TO_OPENAI.get(reason, "stop"))]
        if event.name == "message_stop":
            self.done = True
            out = []
            if self.include_usage:
                prompt = self.usage.input + self.usage.cache_read + self.usage.cache_write
                out.append(openai_event({**self.base, "choices": [], "usage": {
                    "prompt_tokens": prompt, "completion_tokens": self.usage.output,
                    "total_tokens": prompt + self.usage.output,
                    "prompt_tokens_details": {"cached_tokens": self.usage.cache_read}}}))
            return [*out, openai_event(DONE)]
        if event.name == "error":
            self.error, self.done = _upstream_error(payload), True
            return self.error_event(self.error)
        return []  # ping, content_block_start/stop


class OpenAIToAnthropic(Relay):
    """Proveedor compatible con OpenAI, cliente con el contrato de Anthropic (solo texto)."""
    client_contract = Contract.ANTHROPIC

    def __init__(self, model: str, request_id: str):
        super().__init__()
        self.model, self.request_id = model, request_id
        self.started = False
        self.stop_reason = "end_turn"

    def _start(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        return [anthropic_event("message_start", {"type": "message_start", "message": {
                    "id": f"msg_{self.request_id}", "type": "message", "role": "assistant", "model": self.model,
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}}}),
                anthropic_event("content_block_start", {"type": "content_block_start", "index": 0,
                                                        "content_block": {"type": "text", "text": ""}})]

    def feed(self, event: Event) -> list[str]:
        if event.data == DONE:
            self.done = True
            return [*self._start(),
                    anthropic_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    anthropic_event("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": self.stop_reason, "stop_sequence": None},
                        "usage": {"input_tokens": self.usage.input, "output_tokens": self.usage.output,
                                  "cache_read_input_tokens": self.usage.cache_read}}),
                    anthropic_event("message_stop", {"type": "message_stop"})]
        chunk = event.json() or {}
        if chunk.get("error"):
            self.error, self.done = _upstream_error(chunk), True
            return self.error_event(self.error)
        if chunk.get("usage"):
            self.usage = usage_from_openai(chunk)
        out = []
        for choice in chunk.get("choices") or []:
            out += self._start()
            text = (choice.get("delta") or {}).get("content")
            if text:
                out.append(anthropic_event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                                   "delta": {"type": "text_delta", "text": text}}))
            if choice.get("finish_reason"):
                self.stop_reason = STOP_TO_ANTHROPIC.get(choice["finish_reason"], "end_turn")
        return out


def relay_for(ctx: PipelineContext) -> Relay:
    native = native_contract(ctx.target.kind)
    if native == ctx.contract == Contract.ANTHROPIC:
        return AnthropicPassthrough()
    if native == ctx.contract == Contract.OPENAI:
        return OpenAIPassthrough(hide_usage_chunk=ctx.hide_usage_chunk)
    if ctx.contract == Contract.OPENAI:
        include = bool((ctx.body.get("stream_options") or {}).get("include_usage"))
        return AnthropicToOpenAI(ctx.model.key, ctx.request_id, include_usage=include)
    return OpenAIToAnthropic(ctx.model.key, ctx.request_id)


# --- El generador que alimenta la respuesta

async def relay_stream(ctx: PipelineContext, deps: Deps, upstream: httpx.Response) -> AsyncIterator[bytes]:
    relay = relay_for(ctx)
    deadline = perf_counter() + deps.settings.stream_max_seconds
    try:
        try:
            async for event in sse_events(upstream):
                for out in relay.feed(event):
                    yield out.encode("utf-8")
                if relay.done:
                    break
                if perf_counter() > deadline:
                    raise UpstreamTimeout("La respuesta superó la duración máxima del gateway")
            if not relay.done:
                raise UpstreamFailed("El proveedor cortó la respuesta antes de terminar")
        except httpx.TimeoutException:
            ctx.error = UpstreamTimeout("El proveedor dejó de enviar datos")
        except httpx.HTTPError:
            ctx.error = UpstreamFailed("Se perdió la conexión con el proveedor")
        except GatewayError as exc:
            ctx.error = exc
        if ctx.error is not None:
            log.warning("stream.interrupted", request_id=ctx.request_id, provider=ctx.target.provider_slug,
                        error=ctx.error.message)
            for out in relay.error_event(ctx.error):
                yield out.encode("utf-8")
    finally:
        ctx.usage = relay.usage
        if relay.error is not None and ctx.error is None:
            ctx.error = relay.error
        if not relay.done and ctx.error is None:
            ctx.error = GatewayError("El cliente cerró la conexión", status=499, kind="client_disconnected")
        # Al desconectarse el cliente, el generador se cancela: sin el escudo, estas esperas
        # también se cancelarían y la petición quedaría sin cerrar en la auditoría
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
            await audit.close(ctx, deps)
            await quota.settle(ctx, deps)
        ctx.finished = True
        log.info("request.streamed", request_id=ctx.request_id, contract=ctx.contract, model=ctx.model.key,
                 provider=ctx.target.provider_slug, status=ctx.error.status if ctx.error else 200,
                 input_tokens=ctx.usage.input, output_tokens=ctx.usage.output, cost_usd=str(ctx.cost_usd),
                 latency_ms=ctx.elapsed_ms())


def streaming_response(ctx: PipelineContext, deps: Deps, upstream: httpx.Response) -> StreamingResponse:
    return StreamingResponse(relay_stream(ctx, deps, upstream), media_type="text/event-stream", headers={
        "X-Request-ID": ctx.request_id,
        "X-Platia-Model-Used": ctx.model.key,
        "X-Platia-Attempts": str(ctx.attempts),
        **(ctx.quota.headers if ctx.quota else {}),
        "Cache-Control": "no-cache",
        # Que Nginx no acumule la respuesta: cada evento sale en cuanto llega
        "X-Accel-Buffering": "no",
    })
