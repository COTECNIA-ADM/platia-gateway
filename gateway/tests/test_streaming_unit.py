"""El generador de streaming sin base de datos: cortes, tiempos y desconexión del cliente."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app import streaming
from app.contracts import Usage
from app.models import Contract, ProviderKind
from app.pipeline.context import ModelInfo, PipelineContext
from app.providers import Target

from .conftest import ANTHROPIC_SSE, sse


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts, fail_with=None):
        self.parts, self.fail_with = parts, fail_with

    async def __aiter__(self):
        for part in self.parts:
            yield part
        if self.fail_with is not None:
            raise self.fail_with

    async def aclose(self):
        pass


def _ctx(contract=Contract.ANTHROPIC, kind=ProviderKind.ANTHROPIC) -> PipelineContext:
    ctx = PipelineContext(request_id="r1", contract=contract, headers={})
    ctx.target = Target(deployment_id=1, provider_slug="p", kind=kind, base_url=None, upstream_model="u", api_key=None)
    ctx.model = ModelInfo(id=1, key="m", max_output_tokens=None)
    return ctx


@pytest.fixture
def closed(monkeypatch):
    calls = []

    async def fake_close(ctx, deps):
        calls.append((ctx.error, ctx.usage))

    monkeypatch.setattr(streaming.audit, "close", fake_close)
    return calls


def _deps(max_seconds=60.0):
    return SimpleNamespace(settings=SimpleNamespace(stream_max_seconds=max_seconds))


async def _collect(gen) -> str:
    return b"".join([part async for part in gen]).decode()


async def test_cliente_que_se_desconecta(closed):
    ctx = _ctx()
    upstream = httpx.Response(200, stream=Chunks([ANTHROPIC_SSE[:400], ANTHROPIC_SSE[400:]]))
    gen = streaming.relay_stream(ctx, _deps(), upstream)
    first = await gen.__anext__()
    assert first.startswith(b"event: message_start")
    await gen.aclose()  # lo que hace el servidor cuando el cliente cierra
    [(error, usage)] = closed
    assert error.status == 499 and error.kind == "client_disconnected"
    assert usage.input == 1000  # lo leído hasta el corte se registra


@pytest.mark.parametrize("failure, status", [(httpx.ReadTimeout("lento"), 504), (httpx.RemoteProtocolError("x"), 502)])
async def test_fallo_de_red_a_mitad(closed, failure, status):
    upstream = httpx.Response(200, stream=Chunks([ANTHROPIC_SSE[:300]], fail_with=failure))
    text = await _collect(streaming.relay_stream(_ctx(), _deps(), upstream))
    assert text.rstrip().split("\n")[-2] == "event: error"
    assert closed[0][0].status == status


async def test_duracion_maxima(closed):
    upstream = httpx.Response(200, stream=Chunks([ANTHROPIC_SSE]))
    text = await _collect(streaming.relay_stream(_ctx(), _deps(max_seconds=-1), upstream))
    assert "duración máxima" in text and closed[0][0].status == 504


async def test_error_de_openai_a_mitad_hacia_cliente_anthropic(closed):
    upstream = httpx.Response(200, stream=Chunks([sse((None, {"error": {"message": "fallo", "type": "server_error"}}))]))
    ctx = _ctx(Contract.ANTHROPIC, ProviderKind.OPENAI_COMPATIBLE)
    text = await _collect(streaming.relay_stream(ctx, _deps(), upstream))
    assert text.startswith("event: error") and closed[0][0].kind == "server_error"


def test_traductores_ignoran_lo_que_no_es_texto():
    relay = streaming.AnthropicToOpenAI("m", "r", include_usage=False)
    thinking = streaming.Event("content_block_delta", json.dumps({"delta": {"type": "thinking_delta"}}), "")
    assert relay.feed(thinking) == [] and relay.feed(streaming.Event("ping", "{}", "")) == []
    error = relay.feed(streaming.Event("error", json.dumps({"error": {"type": "api_error", "message": "x"}}), ""))
    assert relay.done and json.loads(error[0][6:])["error"]["type"] == "api_error"
    # Un DONE sin contenido previo también abre y cierra el mensaje
    out = streaming.OpenAIToAnthropic("m", "r").feed(streaming.Event(None, "[DONE]", ""))
    assert [line.split("\n")[0] for line in out] == ["event: message_start", "event: content_block_start",
                                                     "event: content_block_stop", "event: message_delta",
                                                     "event: message_stop"]


def test_lectura_de_bloques_sse():
    event = streaming._block(["event: x", "data: {\"a\":", "data: 1}", ": comentario"])
    assert event.name == "x" and event.json() == {"a": 1}
    assert streaming._block(["data: [1]"]).json() is None
    assert streaming._block(["data: no-json"]).json() is None


async def test_ultimo_bloque_sin_linea_en_blanco():
    response = httpx.Response(200, stream=Chunks([b"data: 1\n\ndata: 2"]))
    assert [e.data async for e in streaming.sse_events(response)] == ["1", "2"]


def test_uso_parcial_de_anthropic():
    usage = Usage(input=5)
    from app.contracts import merge_anthropic_usage
    merge_anthropic_usage(usage, {"output_tokens": 9, "input_tokens": None, "cache_read_input_tokens": -1})
    merge_anthropic_usage(usage, None)
    assert usage == Usage(input=5, output=9)
