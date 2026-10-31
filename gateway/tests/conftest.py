"""Utilidades comunes de los tests.

Los tests con base de datos necesitan `TEST_DATABASE_URL` (driver síncrono), por ejemplo
`mysql+pymysql://gateway:pass@127.0.0.1:3307/platia_gateway`. La base se vacía y se
recrea con Alembic: no apuntes nunca a una base con datos. La CI siempre la define.
"""
import base64
import json
import os
from decimal import Decimal
from pathlib import Path

import fakeredis
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.crypto import encrypt, load_key
from app.keys import new_application_key
from app.main import create_app
from app.models import (ApiKey, Application, Deployment, KeyKind, LlmModel, ModelFamily, ModelPrice, Organization,
                        Provider, ProviderKind, Team)
from app.models.base import utcnow
from app.settings import Settings

URL = os.environ.get("TEST_DATABASE_URL")
GATEWAY_DIR = Path(__file__).resolve().parent.parent
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL no definida (la CI la define)")


def alembic_config(connection) -> Config:
    cfg = Config(str(GATEWAY_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(GATEWAY_DIR / "migrations"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


# --- Base de datos con datos de partida y proveedores simulados

KEY_B64 = base64.urlsafe_b64encode(b"k" * 32).decode()
TABLES = ["audit_logs", "api_keys", "model_prices", "deployments", "llm_models", "providers", "applications",
          "users", "teams", "organizations"]

ANTHROPIC_REPLY = {"id": "msg_up", "type": "message", "role": "assistant", "model": "claude-upstream",
                   "content": [{"type": "text", "text": "Hola desde Claude"}], "stop_reason": "end_turn",
                   "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000,
                             "cache_creation_input_tokens": 0}}
OPENAI_REPLY = {"id": "chatcmpl-up", "object": "chat.completion", "model": "gpt-upstream",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hola desde GPT"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 7}}


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL)
    with eng.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    yield eng
    eng.dispose()


def sse(*events: tuple[str | None, dict | str]) -> bytes:
    """Cuerpo SSE: (nombre del evento o None, datos)."""
    out = []
    for name, data in events:
        payload = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {name}\n" if name else "") + f"data: {payload}\n\n")
    return "".join(out).encode()


ANTHROPIC_SSE = sse(
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_up", "type": "message", "role": "assistant", "model": "claude-upstream", "content": [],
        "usage": {"input_tokens": 1000, "output_tokens": 1, "cache_read_input_tokens": 2000}}}),
    ("ping", {"type": "ping"}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hola"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": " mundo"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 500}}),
    ("message_stop", {"type": "message_stop"}),
)


def _openai_chunk(delta: dict, finish=None) -> tuple[None, dict]:
    return None, {"id": "c1", "object": "chat.completion.chunk", "model": "gpt-upstream",
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


OPENAI_SSE = sse(
    _openai_chunk({"role": "assistant", "content": ""}), _openai_chunk({"content": "Hola"}),
    _openai_chunk({"content": " mundo"}), _openai_chunk({}, "stop"),
    (None, {"id": "c1", "object": "chat.completion.chunk", "model": "gpt-upstream", "choices": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 7}}),
    (None, "[DONE]"),
)


class Upstream:
    """Proveedores simulados: guarda lo que recibe y responde lo que se le pide."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.replies: dict[str, httpx.Response | Exception] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.get(request.url.host)
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
            return reply
        anthropic = request.url.host != "openai.test"  # los demás hosts de prueba son de Anthropic
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=ANTHROPIC_SSE if anthropic else OPENAI_SSE,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=ANTHROPIC_REPLY if anthropic else OPENAI_REPLY)

    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def world(engine):
    """Datos de partida; devuelve un objeto con las claves en claro y los ids."""
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in TABLES:
            conn.execute(text(f"TRUNCATE TABLE {table}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    key = load_key(KEY_B64)
    with Session(engine) as db:
        org = Organization(slug="cotecnia", name="Cotecnia")
        db.add(org)
        db.flush()
        team = Team(organization_id=org.id, slug="plataforma", name="Plataforma")
        db.add(team)
        db.flush()
        platia = Application(organization_id=org.id, team_id=team.id, slug="platia", name="PLATIA", trusted=True,
                             allowed_purposes=["proposal_writer"])
        other = Application(organization_id=org.id, team_id=team.id, slug="otra", name="Otra",
                            allowed_models=["gpt-test"])
        db.add_all([platia, other])
        db.flush()
        keys = {"platia": new_application_key("platia"), "otra": new_application_key("otra")}
        for slug, app in (("platia", platia), ("otra", other)):
            db.add(ApiKey(organization_id=org.id, kind=KeyKind.APPLICATION, application_id=app.id,
                          prefix=keys[slug].prefix, key_hash=keys[slug].key_hash))
        anthropic = Provider(slug="anthropic", name="Anthropic", kind=ProviderKind.ANTHROPIC,
                             base_url="https://anthropic.test",
                             api_key_encrypted=encrypt("sk-ant-real", key=key, context="provider:anthropic"))
        openai = Provider(slug="openai", name="OpenAI", kind=ProviderKind.OPENAI_COMPATIBLE,
                          base_url="https://openai.test/v1",
                          api_key_encrypted=encrypt("sk-openai-real", key=key, context="provider:openai"))
        claude = LlmModel(model_key="claude-test", display_name="Claude de prueba", family=ModelFamily.ANTHROPIC,
                          max_output_tokens=8000)
        gpt = LlmModel(model_key="gpt-test", display_name="GPT de prueba", family=ModelFamily.OPENAI)
        db.add_all([anthropic, openai, claude, gpt])
        db.flush()
        db.add_all([Deployment(model_id=claude.id, provider_id=anthropic.id, upstream_model="claude-upstream"),
                    Deployment(model_id=gpt.id, provider_id=openai.id, upstream_model="gpt-upstream"),
                    ModelPrice(model_id=claude.id, effective_from=utcnow().replace(year=2020), input_per_mtok=Decimal(3),
                               output_per_mtok=Decimal(15), cache_read_per_mtok=Decimal("0.3"))])
        db.commit()
        return type("World", (), {"keys": {k: v.raw for k, v in keys.items()}, "org_id": org.id,
                                  "platia_id": platia.id, "claude_id": claude.id})


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def redis_fake():
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def client(world, upstream, redis_fake):
    url = make_url(URL)
    settings = Settings(db_host=url.host, db_port=url.port or 3306, db_user=url.username, db_password=url.password,
                        db_name=url.database, encryption_key=KEY_B64, log_format="console")
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream),
                               redis_client=redis_fake)) as test_client:
        yield test_client
