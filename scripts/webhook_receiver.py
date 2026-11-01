#!/usr/bin/env python3
"""Receptor de webhooks simulado para la prueba en Docker (scripts/prueba_docker.sh).

Guarda cada POST (cabeceras y cuerpo) en una línea de /tmp/hooks.jsonl y responde 200. Con
`python /r.py check <secreto>` comprueba la firma y la fecha de todo lo recibido, como haría
un receptor de verdad (docs/telemetria.md), y escribe los eventos válidos. Solo biblioteca
estándar.
"""
import hashlib
import hmac
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = "/tmp/hooks.jsonl"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - nombre de http.server
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(json.dumps({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}) + "\n")
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def valid(secret: str, header: str, body: str) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    timestamp = int(parts["t"])
    expected = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return abs(time.time() - timestamp) <= 300 and hmac.compare_digest(expected, parts.get("v1", ""))


def check(secret: str) -> None:
    try:
        lines = open(LOG, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        entry = json.loads(line)
        ok = valid(secret, entry["headers"].get("x-platia-signature", "t=0"), entry["body"])
        print(("firma_ok " if ok else "firma_MAL ") + json.loads(entry["body"])["type"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "check":
        check(sys.argv[2])
    else:
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
