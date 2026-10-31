#!/usr/bin/env python3
"""Proveedor simulado para la prueba en Docker (scripts/prueba_docker.sh). No cuesta nada.

Responde como Anthropic en `POST /v1/messages` y como OpenAI en
`POST /v1/chat/completions`, siempre con el mismo texto. Exige la clave de
`FAKE_PROVIDER_KEY` (en `x-api-key` o `Authorization: Bearer`), para comprobar que el
gateway descifra y envía la del proveedor y no la del cliente. Solo biblioteca estándar.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ["FAKE_PROVIDER_KEY"]


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802 - nombre fijado por http.server
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        given = self.headers.get("x-api-key") or self.headers.get("authorization", "").removeprefix("Bearer ")
        if given != KEY:
            self._reply(401, {"error": {"type": "authentication_error", "message": "clave incorrecta"}})
        elif self.path == "/v1/messages":
            self._reply(200, {"id": "msg_fake", "type": "message", "role": "assistant", "model": request.get("model"),
                              "content": [{"type": "text", "text": "Respuesta simulada de Anthropic"}],
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
