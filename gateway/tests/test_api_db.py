"""El contrato de inferencia de punta a punta: MySQL real y proveedores simulados.

Cada test parte de una base limpia (fixture `world` de `conftest.py`) con una
organización, una aplicación de confianza (PLATIA), un proveedor de cada familia y un
modelo en cada uno.
"""
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ApiKey, AuditLog

from .conftest import ANTHROPIC_REPLY, OPENAI_REPLY, needs_db

pytestmark = needs_db


def _audit(engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def _anthropic_body(**extra) -> dict:
    return {"model": "claude-test", "max_tokens": 100, "messages": [{"role": "user", "content": "Hola"}], **extra}


def _openai_body(model="gpt-test", **extra) -> dict:
    return {"model": model, "messages": [{"role": "system", "content": "Breve"}, {"role": "user", "content": "Hola"}],
            **extra}


# --- Caminos buenos

def test_anthropic_a_anthropic_reenvia_el_cuerpo_original(client, world, upstream, engine):
    body = _anthropic_body(thinking={"type": "adaptive"}, output_config={"effort": "high"},
                           tools=[{"name": "t", "input_schema": {"type": "object"}}])
    response = client.post("/v1/messages", json=body, headers={
        "x-api-key": world.keys["platia"], "anthropic-version": "2023-06-01", "anthropic-beta": "beta-x",
        "X-Platia-Purpose": "proposal_writer", "X-Platia-User-Id": "42", "X-Request-ID": "req-anthropic-1"})
    assert response.status_code == 200, response.text
    assert response.json() == ANTHROPIC_REPLY
    sent = upstream.requests[-1]
    assert sent.url == "https://anthropic.test/v1/messages"
    assert sent.headers["x-api-key"] == "sk-ant-real" and sent.headers["anthropic-beta"] == "beta-x"
    assert "x-platia-user-id" not in sent.headers
    assert upstream.last_json() == {**body, "model": "claude-upstream"}
    # 1000*3 + 500*15 + 2000*0.3 = 11100 por millón
    assert response.headers["X-Platia-Cost-Usd"] == "0.01110000"
    assert response.headers["X-Platia-Model-Used"] == "claude-test"
    assert response.headers["X-Request-ID"] == "req-anthropic-1"
    assert int(response.headers["X-Platia-Latency-Ms"]) >= 0

    [row] = _audit(engine)
    assert (row.status, row.http_status, row.request_id) == ("success", 200, "req-anthropic-1")
    assert (row.organization_id, row.application_id, row.purpose, row.end_user_ref) == (
        world.org_id, world.platia_id, "proposal_writer", "42")
    assert (row.contract, row.model_key, row.provider_slug, row.requested_model) == (
        "anthropic", "claude-test", "anthropic", "claude-test")
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (1000, 500, 2000)
    assert row.cost_usd == Decimal("0.01110000") and row.completed_at is not None
    assert row.api_key_prefix.startswith("plgw-app-platia-") and len(row.api_key_prefix) < 30


def test_openai_a_openai_con_bearer(client, world, upstream, engine):
    response = client.post("/v1/chat/completions", json=_openai_body(response_format={"type": "json_object"}),
                           headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert response.status_code == 200, response.text
    assert response.json() == OPENAI_REPLY
    assert upstream.requests[-1].url == "https://openai.test/v1/chat/completions"
    assert upstream.requests[-1].headers["authorization"] == "Bearer sk-openai-real"
    assert upstream.last_json()["response_format"] == {"type": "json_object"}
    # Sin precio configurado el coste es 0, pero los tokens se registran
    assert response.headers["X-Platia-Cost-Usd"] == "0.00000000"
    [row] = _audit(engine)
    assert (row.contract, row.input_tokens, row.output_tokens, row.purpose) == ("openai", 30, 7, "default")


def test_openai_hacia_claude_se_traduce(client, world, upstream):
    response = client.post("/v1/chat/completions", json=_openai_body("claude-test", temperature=0.3),
                           headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert response.status_code == 200, response.text
    assert upstream.last_json() == {"model": "claude-upstream", "max_tokens": 8000, "system": "Breve",
                                    "temperature": 0.3, "messages": [{"role": "user", "content": "Hola"}]}
    out = response.json()
    assert out["object"] == "chat.completion" and out["model"] == "claude-test"
    assert out["choices"][0]["message"]["content"] == "Hola desde Claude"
    assert out["usage"]["prompt_tokens"] == 3000


def test_anthropic_hacia_gpt_se_traduce(client, world, upstream):
    response = client.post("/v1/messages", json={**_anthropic_body(), "model": "gpt-test", "system": "Breve"},
                           headers={"x-api-key": world.keys["platia"]})
    assert response.status_code == 200, response.text
    assert upstream.last_json() == {"model": "gpt-upstream", "max_tokens": 100, "messages": [
        {"role": "system", "content": "Breve"}, {"role": "user", "content": "Hola"}]}
    out = response.json()
    assert out["type"] == "message" and out["content"] == [{"type": "text", "text": "Hola desde GPT"}]


def test_entre_familias_con_herramientas_no_se_degrada(client, world, upstream, engine):
    response = client.post("/v1/chat/completions", json=_openai_body("claude-test", tools=[{"type": "function"}]),
                           headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert response.status_code == 400
    assert "tools" in response.json()["error"]["message"]
    assert upstream.requests == []
    [row] = _audit(engine)
    assert (row.status, row.http_status, row.error_type) == ("error", 400, "invalid_request_error")


def test_modelos_de_la_clave_en_los_dos_formatos(client, world):
    openai = client.get("/v1/models", headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert openai.status_code == 200
    assert [m["id"] for m in openai.json()["data"]] == ["claude-test", "gpt-test"]
    assert openai.json()["data"][0]["owned_by"] == "anthropic"
    anthropic = client.get("/v1/models", headers={"x-api-key": world.keys["platia"], "anthropic-version": "2023-06-01"})
    data = anthropic.json()
    assert data["first_id"] == "claude-test" and data["has_more"] is False
    assert data["data"][0]["display_name"] == "Claude de prueba" and data["data"][0]["created_at"].endswith("Z")
    # "otra" solo puede usar gpt-test
    other = client.get("/v1/models", headers={"Authorization": f"Bearer {world.keys['otra']}"})
    assert [m["id"] for m in other.json()["data"]] == ["gpt-test"]
    bad = client.get("/v1/models", headers={"Authorization": "Bearer plgw-app-x-no"})
    assert bad.status_code == 401


def test_organizacion_limitada_a_un_proveedor(client, world, engine):
    with Session(engine) as db:
        db.execute(text("UPDATE organizations SET allowed_providers = '[\"openai\"]'"))
        db.commit()
    models = client.get("/v1/models", headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert [m["id"] for m in models.json()["data"]] == ["gpt-test"]
    response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    assert response.status_code == 503 and response.json()["error"]["type"] == "overloaded_error"


# --- Identidad

def _set(engine, sql: str) -> None:
    with Session(engine) as db:
        db.execute(text(sql))
        db.commit()


@pytest.mark.parametrize("change", [
    "UPDATE api_keys SET revoked_at = NOW()",
    "UPDATE api_keys SET expires_at = '2020-01-01'",
    "UPDATE applications SET status = 'suspended'",
    "UPDATE organizations SET is_active = 0",
    "UPDATE teams SET deleted_at = NOW()",
])
def test_clave_que_ya_no_vale(client, world, upstream, engine, change):
    _set(engine, change)
    response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Clave API no válida, revocada o caducada"
    assert upstream.requests == [] and _audit(engine) == []


def test_clave_desconocida_y_ultimo_uso(client, world, engine):
    unknown = client.post("/v1/chat/completions", json=_openai_body(),
                          headers={"Authorization": "Bearer plgw-app-platia-falsa"})
    assert unknown.status_code == 401 and unknown.json()["error"]["type"] == "authentication_error"
    assert client.post("/v1/chat/completions", json=_openai_body(),
                       headers={"Authorization": f"Bearer {world.keys['platia']}"}).status_code == 200
    with Session(engine) as db:
        used = db.execute(select(ApiKey.last_used_at).where(ApiKey.application_id == world.platia_id)).scalar_one()
    assert used is not None


def test_cabeceras_platia(client, world, engine):
    headers = {"x-api-key": world.keys["otra"]}
    untrusted = client.post("/v1/chat/completions", json=_openai_body(), headers={**headers, "X-Platia-User-Id": "7"})
    assert untrusted.status_code == 400 and "X-Platia-User-Id" in untrusted.json()["error"]["message"]
    platia = {"x-api-key": world.keys["platia"]}
    bad_user = client.post("/v1/chat/completions", json=_openai_body(), headers={**platia, "X-Platia-User-Id": "a b"})
    assert bad_user.status_code == 400
    bad_purpose = client.post("/v1/chat/completions", json=_openai_body(), headers={**platia, "X-Platia-Purpose": "Mal!"})
    assert bad_purpose.status_code == 400
    denied = client.post("/v1/chat/completions", json=_openai_body(), headers={**platia, "X-Platia-Purpose": "otro"})
    assert denied.status_code == 403 and denied.json()["error"]["type"] == "permission_error"
    rows = _audit(engine)
    assert [r.status for r in rows] == ["error", "error", "error", "blocked"]
    assert rows[-1].purpose == "otro" and rows[0].application_id != world.platia_id


# --- Política y petición

def test_modelo_desconocido_o_no_permitido(client, world, engine):
    unknown = client.post("/v1/messages", json={**_anthropic_body(), "model": "no-existe"},
                          headers={"x-api-key": world.keys["platia"]})
    assert unknown.status_code == 404 and unknown.json()["error"]["type"] == "not_found_error"
    denied = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["otra"]})
    assert denied.status_code == 403
    assert [r.status for r in _audit(engine)] == ["error", "blocked"]


@pytest.mark.parametrize("content, status", [
    (b"no es json", 400), (b"[1, 2]", 400), (b'{"messages": []}', 400), (b"{" + b" " * 11_000_000 + b"}", 413)])
def test_cuerpos_no_validos(client, world, content, status):
    response = client.post("/v1/messages", content=content, headers={"x-api-key": world.keys["platia"],
                                                                     "content-type": "application/json"})
    assert response.status_code == status


def test_modelo_sin_destino(client, world, engine):
    _set(engine, "UPDATE deployments SET is_active = 0")
    response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    assert response.status_code == 503 and "destino" in response.json()["error"]["message"]


def test_clave_de_proveedor_ilegible(client, world, upstream, engine):
    _set(engine, "UPDATE providers SET api_key_encrypted = 'enc:v1:AAAA' WHERE slug = 'anthropic'")
    response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    assert response.status_code == 502 and "sk-" not in response.text
    assert upstream.requests == []


# --- Errores del proveedor

@pytest.mark.parametrize("reply, status, kind", [
    (httpx.Response(400, json={"error": {"message": "max_tokens: Field required"}}), 400, "invalid_request_error"),
    (httpx.Response(529, json={"error": {"message": "Overloaded"}}), 503, "overloaded_error"),
    (httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}), 502, "api_error"),
    (httpx.ReadTimeout("lento"), 504, "api_error"),
])
def test_errores_del_proveedor(client, world, upstream, engine, reply, status, kind):
    upstream.replies["anthropic.test"] = reply
    response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    assert response.status_code == status
    assert response.json()["error"]["type"] == kind
    [row] = _audit(engine)
    assert (row.status, row.http_status, row.provider_slug) == ("error", status, "anthropic")
    assert row.completed_at is not None


def test_si_no_se_puede_auditar_no_se_llama_al_proveedor(client, world, upstream, engine):
    # Sin la tabla de auditoría la apertura falla: la petición no debe salir
    _set(engine, "RENAME TABLE audit_logs TO audit_logs_fuera")
    try:
        response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    finally:
        _set(engine, "RENAME TABLE audit_logs_fuera TO audit_logs")
    assert response.status_code == 503
    assert upstream.requests == []



def test_fallo_de_la_base_a_mitad_da_503_sin_detalles(client, world, engine):
    _set(engine, "RENAME TABLE llm_models TO llm_models_fuera")
    try:
        response = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": world.keys["platia"]})
    finally:
        _set(engine, "RENAME TABLE llm_models_fuera TO llm_models")
    assert response.status_code == 503
    assert "llm_models" not in response.text and "SELECT" not in response.text


def test_error_inesperado_da_500_sin_detalles(client, world, engine, monkeypatch):
    async def boom(ctx, deps):
        raise RuntimeError("contraseña=secreta")

    monkeypatch.setattr("app.pipeline.execute.run", boom)
    response = client.post("/v1/chat/completions", json=_openai_body(),
                           headers={"Authorization": f"Bearer {world.keys['platia']}"})
    assert response.status_code == 500 and "secreta" not in response.text
    assert response.json()["error"]["type"] == "api_error"
    [row] = _audit(engine)
    assert (row.status, row.http_status) == ("error", 500)


def test_comodin_de_propositos(client, world, engine):
    _set(engine, "UPDATE applications SET allowed_purposes = '[\"*\"]' WHERE slug = 'otra'")
    response = client.post("/v1/chat/completions", json=_openai_body(),
                           headers={"Authorization": f"Bearer {world.keys['otra']}",
                                    "X-Platia-Purpose": "agent_copilot"})
    assert response.status_code == 200
    assert _audit(engine)[-1].purpose == "agent_copilot"
