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

from .content import TextGuard, find, tally
from .content_policy import flag
from .content_policy import header as content_header
from .contracts import (STOP_TO_ANTHROPIC, STOP_TO_OPENAI, Usage, merge_anthropic_usage, native_contract,
                        usage_from_openai)
from .errors import ContentBlocked, GatewayError, UpstreamFailed, UpstreamTimeout
from .models import ContentAction, ContentCheck, Contract, Direction
from . import telemetry
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


# --- Filtro de la salida (G8, ADR-012)

class OutputFilter:
    """Aplica la política de salida a los eventos que van al cliente, ya en su contrato.

    Solo con `log` los eventos salen tal cual y el texto se revisa al final. Con `redact` o
    `block`, el texto de cada bloque pasa por un `TextGuard`: se reescriben los eventos de
    texto con lo que ya se puede enviar y el resto sale al cerrar el bloque. Con `block`, al
    encontrar algo se corta la respuesta: lo retenido no llega al cliente.
    """
    CHECKS = (ContentCheck.SECRET, ContentCheck.PII)

    def __init__(self, ctx: PipelineContext):
        self.ctx = ctx
        actions = {c: ctx.content_actions.get((Direction.OUTPUT, c), ContentAction.OFF) for c in self.CHECKS}
        self.actions = actions
        self.checks = {c for c, a in actions.items() if a != ContentAction.OFF}
        self.block = {c for c, a in actions.items() if a == ContentAction.BLOCK}
        self.rewrite = bool(self.block or any(a == ContentAction.REDACT for a in actions.values()))
        redact_checks = {c for c, a in actions.items() if a == ContentAction.REDACT}
        self.guards: dict[int, TextGuard] = {}
        self.new_guard = lambda: TextGuard(self.checks, redact_checks)
        self.seen: list[str] = []  # sin reescritura: el texto, para revisarlo al final
        self.anthropic_index = 0
        self.openai_base: dict = {}

    def _guard(self, index: int) -> TextGuard:
        return self.guards.setdefault(index, self.new_guard())

    def _checked(self, guard: TextGuard, before: int, text: str) -> str:
        if any(f.check in self.block for f in guard.found[before:]):
            raise ContentBlocked("La respuesta del modelo contenía datos que la política de contenido no permite "
                                 "enviar; se ha cortado")
        return text

    def _feed(self, index: int, text: str) -> str:
        guard = self._guard(index)
        before = len(guard.found)
        return self._checked(guard, before, guard.feed(text))

    def _flush(self, index: int) -> str:
        guard = self.guards.get(index)
        if guard is None:
            return ""
        before = len(guard.found)
        return self._checked(guard, before, guard.flush())

    def process(self, out: str) -> list[str]:
        if not self.checks:
            return [out]
        event = _block(out.rstrip("\n").split("\n"))
        if self.ctx.contract == Contract.ANTHROPIC:
            return self._anthropic(event, out)
        return self._openai(event, out)

    def _anthropic(self, event: Event, out: str) -> list[str]:
        payload = event.json() or {}
        delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        if event.name == "content_block_delta" and delta.get("type") == "text_delta":
            text = delta.get("text") or ""
            if not self.rewrite:
                self.seen.append(text)
                return [out]
            self.anthropic_index = payload.get("index", 0)
            safe = self._feed(self.anthropic_index, text)
            if not safe:
                return []
            return [anthropic_event("content_block_delta", {**payload, "delta": {**delta, "text": safe}})]
        if self.rewrite and event.name in ("content_block_stop", "message_delta", "message_stop"):
            index = payload.get("index", self.anthropic_index) if event.name == "content_block_stop" \
                else self.anthropic_index
            rest = self._flush(index)
            if rest:
                return [anthropic_event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                                "delta": {"type": "text_delta", "text": rest}}), out]
        return [out]

    def _openai(self, event: Event, out: str) -> list[str]:
        chunk = None if event.data == DONE else event.json()
        if chunk is None:
            return [*self._openai_flush_all(), out] if self.rewrite else [out]
        self.openai_base = {k: chunk[k] for k in ("id", "object", "created", "model") if k in chunk}
        before: list[str] = []
        changed = False
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            index = choice.get("index", 0)
            if isinstance(delta.get("content"), str):
                if not self.rewrite:
                    self.seen.append(delta["content"])
                else:
                    delta["content"] = self._feed(index, delta["content"])
                    changed = True
            if self.rewrite and choice.get("finish_reason"):
                rest = self._flush(index)
                if rest:
                    before.append(self._openai_text(index, rest))
        if not changed:
            return [*before, out]
        return [*before, openai_event(chunk)]

    def _openai_text(self, index: int, text: str) -> str:
        return openai_event({**self.openai_base, "choices": [{"index": index, "delta": {"content": text},
                                                              "finish_reason": None}]})

    def _openai_flush_all(self) -> list[str]:
        return [self._openai_text(i, rest) for i in list(self.guards) if (rest := self._flush(i))]

    def finish(self) -> None:
        """Anota lo encontrado en la salida (también si la respuesta se cortó)."""
        if not self.checks:
            return
        findings = [f for g in self.guards.values() for f in g.found] if self.rewrite \
            else find("".join(self.seen), self.checks)
        for check, kinds in tally(findings).items():
            flag(self.ctx, Direction.OUTPUT, ContentCheck(check), self.actions[ContentCheck(check)], kinds)


# --- El generador que alimenta la respuesta

async def relay_stream(ctx: PipelineContext, deps: Deps, upstream: httpx.Response) -> AsyncIterator[bytes]:
    relay = relay_for(ctx)
    output_filter = OutputFilter(ctx)
    deadline = perf_counter() + deps.settings.stream_max_seconds
    try:
        try:
            async for event in sse_events(upstream):
                for out in relay.feed(event):
                    for piece in output_filter.process(out):
                        yield piece.encode("utf-8")
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
        output_filter.finish()
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
            await telemetry.finish(ctx, deps)
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
        **content_header(ctx),  # lo encontrado en la entrada; lo de la salida, en la auditoría
        "Cache-Control": "no-cache",
        # Que Nginx no acumule la respuesta: cada evento sale en cuanto llega
        "X-Accel-Buffering": "no",
    })
