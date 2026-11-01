#!/usr/bin/env python3
"""Proveedor simulado para la prueba en Docker (scripts/prueba_docker.sh). No cuesta nada.

Responde como Anthropic en `POST /v1/messages` y como OpenAI en
`POST /v1/chat/completions`, siempre con el mismo texto, con o sin streaming (SSE). Para
probar la reserva (G6), el modelo `caido` responde 503 y `saturado`, 429 con Retry-After. Para
la seguridad de contenido (G8), el modelo `eco` devuelve lo que recibió (en streaming, en
trozos de 7 caracteres): así se ve qué llega al proveedor y qué filtra la salida. Exige la clave de
`FAKE_PROVIDER_KEY` (en `x-api-key` o `Authorization: Bearer`), para comprobar que el
gateway descifra y envía la del proveedor y no la del cliente. Solo biblioteca estándar.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ["FAKE_PROVIDER_KEY"]
WORDS = ["Respuesta ", "simulada ", "en ", "streaming"]


def echo_of(request: dict) -> str:
    content = (request.get("messages") or [{}])[-1].get("content")
    return "Has escrito: " + (content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))


def anthropic_events(model: str, words: list[str] = WORDS) -> list[str]:
    def event(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"
    out = [event("message_start", {"type": "message_start", "message": {
               "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}}),
           event("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
           event("ping", {"type": "ping"})]
    out += [event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": w}}) for w in words]
    return out + [event("content_block_stop", {"type": "content_block_stop", "index": 0}),
                  event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                             "stop_sequence": None},
                                          "usage": {"output_tokens": 200}}),
                  event("message_stop", {"type": "message_stop"})]


def openai_events(model: str, include_usage: bool) -> list[str]:
    def chunk(delta, finish=None):
        return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    chunks = [chunk({"role": "assistant", "content": ""})] + [chunk({"content": w}) for w in WORDS]
    chunks.append(chunk({}, "stop"))
    if include_usage:
        chunks.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": model,
                       "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}})
    return [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, events: list[str]) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for item in events:
            self.wfile.write(item.encode())
            self.wfile.flush()
            time.sleep(0.02)  # que se note que llega por partes

    def do_POST(self):  # noqa: N802 - nombre fijado por http.server
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        given = self.headers.get("x-api-key") or self.headers.get("authorization", "").removeprefix("Bearer ")
        if given != KEY:
            self._reply(401, {"error": {"type": "authentication_error", "message": "clave incorrecta"}})
        elif request.get("model") == "caido":
            self._reply(503, {"error": {"type": "overloaded_error", "message": "servicio caído"}})
        elif request.get("model") == "saturado":
            self.send_response(429)
            self.send_header("retry-after", "20")
            self.send_header("content-length", "0")
            self.end_headers()
        elif self.path == "/v1/messages" and request.get("stream"):
            words = WORDS
            if request.get("model") == "eco":
                text = echo_of(request)
                words = [text[i:i + 7] for i in range(0, len(text), 7)]
            self._stream(anthropic_events(request.get("model"), words))
        elif self.path == "/v1/chat/completions" and request.get("stream"):
            include = bool((request.get("stream_options") or {}).get("include_usage"))
            self._stream(openai_events(request.get("model"), include))
        elif self.path == "/v1/messages":
            self._reply(200, {"id": "msg_fake", "type": "message", "role": "assistant", "model": request.get("model"),
                              "content": [{"type": "text", "text": echo_of(request) if request.get("model") == "eco"
                                           else "Respuesta simulada de Anthropic"}],
                              "stop_reason": "end_turn", "usage": {"input_tokens": 1000, "output_tokens": 200}})
        elif self.path == "/v1/chat/completions":
            self._reply(200, {"id": "chatcmpl-fake", "object": "chat.completion", "model": request.get("model"),
                              "choices": [{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": "Respuesta simulada de OpenAI"}}],
                              "usage": {"prompt_tokens": 50, "completion_tokens": 10}})
        else:
            self._reply(404, {"error": {"message": "ruta desconocida"}})

    def log_message(self, *args) -> None:  # silencio: la prueba mira las respuestas
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
