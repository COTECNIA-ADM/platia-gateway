"""Reserva y enfriamiento (G6): MySQL real, Redis en memoria y proveedores simulados.

Parte de `world` (conftest.py): `claude-test` en anthropic.test y `gpt-test` en
openai.test. Cada test añade los destinos que necesita.
"""
import dataclasses

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.crypto import encrypt, load_key
from app.models import Application, AuditLog, Deployment, LlmModel, Provider, ProviderKind

from .conftest import ANTHROPIC_SSE, KEY_B64, needs_db

pytestmark = needs_db


def _audit(engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def _add_anthropic(engine, world, host: str, *, priority=0, weight=100) -> int:
    """Otro proveedor Anthropic (en otro host) que sirve claude-test; devuelve el id del despliegue."""
    slug = host.split(".")[0]
    with Session(engine) as db:
        provider = Provider(slug=slug, name=slug, kind=ProviderKind.ANTHROPIC, base_url=f"https://{host}",
                            api_key_encrypted=encrypt(f"sk-{slug}", key=load_key(KEY_B64), context=f"provider:{slug}"))
        db.add(provider)
        db.flush()
        deployment = Deployment(model_id=world.claude_id, provider_id=provider.id, upstream_model="claude-upstream",
                                priority=priority, weight=weight)
        db.add(deployment)
        db.commit()
        return deployment.id


def _set_priority(engine, host_slug: str, priority: int) -> None:
    with Session(engine) as db:
        provider = db.execute(select(Provider).filter_by(slug=host_slug)).scalar_one()
        db.execute(update(Deployment).where(Deployment.provider_id == provider.id).values(priority=priority))
        db.commit()


def _fallbacks(engine, model_key: str, chain: list[str]) -> None:
    with Session(engine) as db:
        db.execute(update(LlmModel).where(LlmModel.model_key == model_key).values(fallback_models=chain))
        db.commit()


def _post(client, world, app="platia", **extra):
    body = {"model": "claude-test", "max_tokens": 100, "messages": [{"role": "user", "content": "Hola"}], **extra}
    return client.post("/v1/messages", json=body, headers={"x-api-key": world.keys[app]})


def _hosts(upstream) -> list[str]:
    return [r.url.host for r in upstream.requests]


def _settings(client, **changes) -> None:
    deps = client.app.state.deps
    client.app.state.deps = dataclasses.replace(deps, settings=dataclasses.replace(deps.settings, **changes))


# --- Reserva entre despliegues del mismo modelo

def test_si_el_primero_falla_responde_el_siguiente(client, world, upstream, engine):
    backup = _add_anthropic(engine, world, "backup.test", priority=1)
    upstream.replies["anthropic.test"] = httpx.Response(503, json={"error": {"message": "caído"}})
    response = _post(client, world)
    assert response.status_code == 200, response.text
    assert response.headers["X-Platia-Attempts"] == "2"
    assert response.headers["X-Platia-Model-Used"] == "claude-test"
    assert _hosts(upstream) == ["anthropic.test", "backup.test"]
    assert upstream.requests[-1].headers["x-api-key"] == "sk-backup"  # la clave de su proveedor
    [row] = _audit(engine)
    assert (row.status, row.attempts, row.deployment_id, row.provider_slug) == ("success", 2, backup, "backup")
    assert row.input_tokens == 1000 and row.cost_usd > 0


def test_un_error_de_la_peticion_no_cambia_de_destino(client, world, upstream, engine):
    _add_anthropic(engine, world, "backup.test", priority=1)
    upstream.replies["anthropic.test"] = httpx.Response(400, json={"error": {"message": "max_tokens no válido"}})
    response = _post(client, world)
    assert response.status_code == 400
    assert _hosts(upstream) == ["anthropic.test"]
    [row] = _audit(engine)
    assert (row.status, row.attempts) == ("error", 1)


def test_si_fallan_todos_se_devuelve_el_ultimo_error(client, world, upstream, engine):
    _add_anthropic(engine, world, "backup.test", priority=1)
    upstream.replies["anthropic.test"] = httpx.Response(503)
    upstream.replies["backup.test"] = httpx.ConnectError("sin red")
    response = _post(client, world)
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "No se pudo contactar con el proveedor"
    [row] = _audit(engine)
    assert (row.status, row.http_status, row.attempts, row.provider_slug) == ("error", 502, 2, "backup")


def test_como_mucho_route_max_attempts_destinos(client, world, upstream, engine):
    _add_anthropic(engine, world, "b1.test", priority=1)
    _add_anthropic(engine, world, "b2.test", priority=2)
    _settings(client, route_max_attempts=2)
    for host in ("anthropic.test", "b1.test", "b2.test"):
        upstream.replies[host] = httpx.Response(500)
    assert _post(client, world).status_code == 502
    assert _hosts(upstream) == ["anthropic.test", "b1.test"]


def test_pasada_la_ventana_no_se_empieza_otro_intento(client, world, upstream, engine):
    _add_anthropic(engine, world, "backup.test", priority=1)
    _settings(client, route_retry_window_seconds=0)
    upstream.replies["anthropic.test"] = httpx.Response(503)
    assert _post(client, world).status_code == 503
    assert _hosts(upstream) == ["anthropic.test"]
    assert _audit(engine)[0].attempts == 1


def test_la_reserva_tambien_en_streaming(client, world, upstream, engine):
    _add_anthropic(engine, world, "backup.test", priority=1)
    upstream.replies["anthropic.test"] = httpx.Response(529, json={"error": {"message": "saturado"}})
    upstream.replies["backup.test"] = httpx.Response(200, content=ANTHROPIC_SSE,
                                                     headers={"content-type": "text/event-stream"})
    response = _post(client, world, stream=True)
    assert response.status_code == 200
    assert response.headers["X-Platia-Attempts"] == "2"
    assert "event: message_stop" in response.text
    [row] = _audit(engine)
    assert (row.status, row.stream, row.attempts, row.provider_slug, row.output_tokens) == (
        "success", True, 2, "backup", 500)


# --- Enfriamiento

async def _cooled(redis_fake, deployment_id: int):
    return await redis_fake.get(f"gw:cooldown:{deployment_id}"), await redis_fake.ttl(f"gw:cooldown:{deployment_id}")


def test_un_429_enfria_el_despliegue_lo_que_pide_el_proveedor(client, world, upstream, engine, redis_fake):
    _add_anthropic(engine, world, "backup.test", priority=1)
    upstream.replies["anthropic.test"] = httpx.Response(429, headers={"retry-after": "30"})
    assert _post(client, world).status_code == 200
    with Session(engine) as db:
        first = db.execute(select(Deployment.id).where(Deployment.model_id == world.claude_id,
                                                     Deployment.priority == 0)).scalar_one()
    value, ttl = client.portal.call(_cooled, redis_fake, first)
    assert value == b"rate_limit" and 25 <= ttl <= 30

    # Mientras está enfriado, va al final de la cola: la siguiente petición empieza por la reserva
    upstream.requests.clear()
    del upstream.replies["anthropic.test"]
    response = _post(client, world)
    assert response.headers["X-Platia-Attempts"] == "1"
    assert _hosts(upstream) == ["backup.test"]


def test_un_enfriado_se_usa_si_no_queda_otro(client, world, upstream, engine, redis_fake):
    upstream.replies["anthropic.test"] = httpx.Response(429)
    assert _post(client, world).status_code == 503
    del upstream.replies["anthropic.test"]
    # El único destino de claude-test está enfriado, pero se prueba igual: mejor que no dar servicio
    assert _post(client, world).status_code == 200


def test_fallos_seguidos_enfrian_y_un_exito_reinicia_la_cuenta(client, world, upstream, engine, redis_fake):
    _settings(client, cooldown_failures=2, cooldown_seconds=45)
    with Session(engine) as db:
        deployment = db.execute(select(Deployment.id).where(Deployment.model_id == world.claude_id)).scalar_one()
    upstream.replies["anthropic.test"] = httpx.Response(500)
    _post(client, world)
    del upstream.replies["anthropic.test"]
    _post(client, world)  # un éxito: la cuenta vuelve a cero
    upstream.replies["anthropic.test"] = httpx.Response(500)
    _post(client, world)
    assert client.portal.call(_cooled, redis_fake, deployment)[0] is None
    _post(client, world)  # segundo fallo seguido
    value, ttl = client.portal.call(_cooled, redis_fake, deployment)
    assert value == b"api_error" and 40 <= ttl <= 45


def test_sin_redis_se_sigue_sin_enfriamiento(client, world, upstream, engine):
    class Broken:
        async def mget(self, *args, **kwargs):
            raise ConnectionError("sin redis")
        set = delete = incr = expire = mget
    deps = client.app.state.deps
    client.app.state.deps = dataclasses.replace(deps, redis=Broken())
    _add_anthropic(engine, world, "backup.test", priority=1)
    upstream.replies["anthropic.test"] = httpx.Response(429)
    response = _post(client, world)
    assert response.status_code == 200 and response.headers["X-Platia-Attempts"] == "2"


# --- Reserva entre modelos

def test_reserva_hacia_otro_modelo_de_otra_familia(client, world, upstream, engine):
    _fallbacks(engine, "claude-test", ["gpt-test"])
    upstream.replies["anthropic.test"] = httpx.TimeoutException("lento")
    response = _post(client, world)
    assert response.status_code == 200, response.text
    assert response.headers["X-Platia-Model-Used"] == "gpt-test"
    # Traducida a OpenAI y devuelta en el contrato de Anthropic que pidió el cliente
    assert upstream.last_json()["model"] == "gpt-upstream"
    assert response.json()["content"][0]["text"] == "Hola desde GPT"
    [row] = _audit(engine)
    assert (row.requested_model, row.model_key, row.provider_slug, row.attempts) == (
        "claude-test", "gpt-test", "openai", 2)
    assert row.input_tokens == 30


def test_la_reserva_no_traduce_si_se_perderia_algo(client, world, upstream, engine):
    _fallbacks(engine, "claude-test", ["gpt-test"])
    upstream.replies["anthropic.test"] = httpx.Response(503)
    response = _post(client, world, tools=[{"name": "t", "input_schema": {"type": "object"}}])
    assert response.status_code == 503
    assert _hosts(upstream) == ["anthropic.test"]


def test_la_reserva_respeta_los_modelos_permitidos(client, world, upstream, engine):
    # «otra» solo puede usar gpt-test: claude-test como reserva no le vale
    _fallbacks(engine, "gpt-test", ["claude-test"])
    upstream.replies["openai.test"] = httpx.Response(503)
    response = _post(client, world, app="otra", model="gpt-test")
    assert response.status_code == 503
    assert _hosts(upstream) == ["openai.test"]

    with Session(engine) as db:
        db.execute(update(Application).where(Application.slug == "otra")
                   .values(allowed_models=["gpt-test", "claude-test"]))
        db.commit()
    upstream.requests.clear()
    response = _post(client, world, app="otra", model="gpt-test")
    assert response.status_code == 200 and response.headers["X-Platia-Model-Used"] == "claude-test"


def test_la_reserva_ignora_modelos_inactivos_o_desconocidos(client, world, upstream, engine):
    _fallbacks(engine, "claude-test", ["no-existe", "gpt-test"])
    with Session(engine) as db:
        db.execute(update(LlmModel).where(LlmModel.model_key == "gpt-test").values(is_active=False))
        db.commit()
    upstream.replies["anthropic.test"] = httpx.Response(503)
    assert _post(client, world).status_code == 503
    assert _hosts(upstream) == ["anthropic.test"]


def test_peticion_que_solo_sirve_la_reserva(client, world, upstream, engine):
    # gpt-test se sirve en OpenAI: con herramientas de Anthropic no es elegible, pero su
    # reserva claude-test sí, y se usa directamente
    _fallbacks(engine, "gpt-test", ["claude-test"])
    response = _post(client, world, model="gpt-test", tools=[{"name": "t", "input_schema": {"type": "object"}}])
    assert response.status_code == 200
    assert response.headers["X-Platia-Model-Used"] == "claude-test"
    assert response.headers["X-Platia-Attempts"] == "1"


def test_peso_cero_solo_como_reserva(client, world, upstream, engine):
    _add_anthropic(engine, world, "zero.test", priority=0, weight=0)
    _set_priority(engine, "anthropic", 0)
    for _ in range(5):
        _post(client, world)
    assert set(_hosts(upstream)) == {"anthropic.test"}
