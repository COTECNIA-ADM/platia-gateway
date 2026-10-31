"""Streaming de punta a punta: MySQL real y proveedores simulados que responden SSE."""
import json
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog

from .conftest import ANTHROPIC_SSE, OPENAI_SSE, needs_db, sse

pytestmark = needs_db


def _audit(engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def _events(text: str) -> list[tuple[str | None, str]]:
    """[(evento, datos)] de un cuerpo SSE."""
    out = []
    for block in text.strip().split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        out.append((name, "\n".join(data)))
    return out


def _openai_text(text: str) -> str:
    chunks = [json.loads(d) for _, d in _events(text) if d != "[DONE]"]
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))


def _anthropic(world, **body):
    return {"x-api-key": world.keys["platia"]}, {
        "model": "claude-test", "max_tokens": 100, "stream": True, "messages": [{"role": "user", "content": "Hola"}],
        **body}


def test_anthropic_a_anthropic_pasa_cada_evento_tal_cual(client, world, upstream, engine):
    headers, body = _anthropic(world, thinking={"type": "adaptive"})
    response = client.post("/v1/messages", json=body, headers={**headers, "X-Request-ID": "req-stream-1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-platia-model-used"] == "claude-test"
    assert response.headers["x-accel-buffering"] == "no" and response.headers["x-request-id"] == "req-stream-1"
    assert response.content == ANTHROPIC_SSE
    assert json.loads(upstream.requests[-1].content) == {**body, "model": "claude-upstream"}
    assert upstream.requests[-1].headers["accept"] == "text/event-stream"
    [row] = _audit(engine)
    assert (row.status, row.http_status, row.stream) == ("success", 200, True)
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (1000, 500, 2000)
    assert row.cost_usd == Decimal("0.01110000")


def test_openai_a_openai_pide_el_uso_sin_enseñarlo(client, world, upstream, engine):
    body = {"model": "gpt-test", "stream": True, "messages": [{"role": "user", "content": "Hola"}]}
    response = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert response.status_code == 200
    assert json.loads(upstream.requests[-1].content)["stream_options"] == {"include_usage": True}
    assert _openai_text(response.text) == "Hola mundo"
    assert '"usage"' not in response.text and response.text.endswith("data: [DONE]\n\n")
    [row] = _audit(engine)
    assert (row.status, row.input_tokens, row.output_tokens) == ("success", 30, 7)

    with_usage = client.post("/v1/chat/completions", json={**body, "stream_options": {"include_usage": True}},
                             headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert with_usage.content == OPENAI_SSE


def test_openai_hacia_claude_en_streaming(client, world, upstream, engine):
    body = {"model": "claude-test", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "Hola"}]}
    response = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert response.status_code == 200
    sent = json.loads(upstream.requests[-1].content)
    assert sent["stream"] is True and "stream_options" not in sent
    events = _events(response.text)
    assert events[-1] == (None, "[DONE]")
    chunks = [json.loads(d) for _, d in events[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert _openai_text(response.text) == "Hola mundo"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["prompt_tokens"] == 3000
    assert {c["model"] for c in chunks} == {"claude-test"}
    [row] = _audit(engine)
    assert (row.status, row.output_tokens) == ("success", 500)


def test_anthropic_hacia_gpt_en_streaming(client, world, upstream, engine):
    headers, body = _anthropic(world, model="gpt-test")
    response = client.post("/v1/messages", json=body, headers=headers)
    assert response.status_code == 200
    assert json.loads(upstream.requests[-1].content)["stream_options"] == {"include_usage": True}
    events = _events(response.text)
    assert [name for name, _ in events] == ["message_start", "content_block_start", "content_block_delta",
                                            "content_block_delta", "content_block_stop", "message_delta",
                                            "message_stop"]
    assert "".join(json.loads(d)["delta"]["text"] for n, d in events if n == "content_block_delta") == "Hola mundo"
    delta = json.loads(events[-2][1])
    assert delta["delta"]["stop_reason"] == "end_turn" and delta["usage"]["output_tokens"] == 7
    [row] = _audit(engine)
    assert (row.status, row.input_tokens, row.output_tokens) == ("success", 30, 7)


def test_error_antes_del_primer_evento_es_un_error_http(client, world, upstream, engine):
    upstream.replies["anthropic.test"] = httpx.Response(529, json={"error": {"message": "Overloaded"}})
    headers, body = _anthropic(world)
    response = client.post("/v1/messages", json=body, headers=headers)
    assert response.status_code == 503 and response.json()["error"]["type"] == "overloaded_error"
    [row] = _audit(engine)
    assert (row.status, row.http_status, row.stream) == ("error", 503, True)


def test_error_a_mitad_se_reenvia_y_se_audita(client, world, upstream, engine):
    upstream.replies["anthropic.test"] = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})))
    headers, body = _anthropic(world)
    response = client.post("/v1/messages", json=body, headers=headers)
    assert response.status_code == 200
    assert [name for name, _ in _events(response.text)] == ["message_start", "error"]
    [row] = _audit(engine)
    assert (row.status, row.error_type, row.input_tokens) == ("error", "overloaded_error", 10)


def test_proveedor_que_corta_sin_terminar(client, world, upstream, engine):
    upstream.replies["openai.test"] = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                     content=sse((None, {"choices": [{"delta": {"content": "Ho"}}]})))
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {world.keys['platia']}"},
                           json={"model": "gpt-test", "stream": True, "messages": [{"role": "user", "content": "x"}]})
    events = _events(response.text)
    assert json.loads(events[-1][1])["error"]["message"].startswith("El proveedor cortó")
    [row] = _audit(engine)
    assert (row.status, row.http_status) == ("error", 502)
