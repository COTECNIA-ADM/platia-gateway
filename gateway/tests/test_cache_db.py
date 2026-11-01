"""Caché exacta de respuestas (G11, ADR-015)."""
from decimal import Decimal

import fakeredis
import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import cli
from app.models import (Application, AuditLog, ContentPolicy, LimitMetric, LimitPeriod, LimitScope, PolicyScope,
                        UsageLimit)
from app.pipeline import cache

from .conftest import ANTHROPIC_REPLY, OPENAI_REPLY, needs_db

pytestmark = needs_db
EMAIL = "ana.garcia@cotecnia.es"


def _cache(engine, world, ttl=60, purposes=None):
    with Session(engine) as db:
        db.execute(update(Application).where(Application.id == world.platia_id)
                   .values(cache_ttl_seconds=ttl, cache_purposes=purposes))
        db.commit()


def _post(client, world, text="Hola", *, user=None, headers=None, contract="anthropic", **extra):
    hdrs = {"x-api-key": world.keys["platia"], **(headers or {})}
    if user:
        hdrs["X-Platia-User-Id"] = user
    model = "claude-test" if contract == "anthropic" else "gpt-test"
    body = {"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": text}], **extra}
    path = "/v1/messages" if contract == "anthropic" else "/v1/chat/completions"
    return client.post(path, json=body, headers=hdrs)


def _audit(engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def test_segunda_peticion_igual_sale_de_la_cache(client, world, upstream, engine, redis_fake):
    _cache(engine, world)
    first = _post(client, world, user="7")
    second = _post(client, world, user="7")
    assert first.headers["X-Platia-Cache"] == "miss" and second.headers["X-Platia-Cache"] == "hit"
    assert second.json() == first.json()
    assert len(upstream.requests) == 1
    assert second.headers["X-Platia-Cost-Usd"] == "0.00000000" and second.headers["X-Platia-Attempts"] == "0"
    rows = _audit(engine)
    assert [r.provider_slug for r in rows] == ["anthropic", "cache"]
    assert (rows[1].input_tokens, rows[1].output_tokens, rows[1].cost_usd) == (0, 0, Decimal("0"))
    assert rows[1].status == "success" and rows[1].model_key == "claude-test"


def test_respuesta_cifrada_en_redis(client, world, engine, redis_fake):
    _cache(engine, world)
    _post(client, world)
    import asyncio

    async def read():
        keys = [k async for k in redis_fake.scan_iter(match="gw:cache:*")]
        return keys, [await redis_fake.get(k) for k in keys]

    keys, values = asyncio.run(read())
    assert len(keys) == 1 and keys[0].decode().startswith(f"gw:cache:{world.org_id}:{world.platia_id}:")
    assert values[0].startswith(b"enc:v1:") and b"Hola desde Claude" not in values[0]


def test_cada_persona_su_cache(client, world, upstream, engine):
    _cache(engine, world)
    _post(client, world, user="7")
    assert _post(client, world, user="8").headers["X-Platia-Cache"] == "miss"
    assert _post(client, world).headers["X-Platia-Cache"] == "miss"  # sin persona: la de la aplicación
    assert len(upstream.requests) == 3


def test_otra_peticion_otro_proposito_u_otra_cabecera_no_coinciden(client, world, upstream, engine):
    _cache(engine, world)
    _post(client, world, "Hola")
    assert _post(client, world, "Adiós").headers["X-Platia-Cache"] == "miss"
    assert _post(client, world, headers={"X-Platia-Purpose": "proposal_writer"}).headers["X-Platia-Cache"] == "miss"
    assert _post(client, world, headers={"anthropic-beta": "algo"}).headers["X-Platia-Cache"] == "miss"
    assert _post(client, world, "Hola").headers["X-Platia-Cache"] == "hit"
    assert len(upstream.requests) == 4


def test_sin_cache_por_defecto_por_cabecera_y_en_streaming(client, world, upstream, engine):
    assert "X-Platia-Cache" not in _post(client, world).headers  # 0 segundos: sin caché
    _cache(engine, world)
    _post(client, world)
    assert "X-Platia-Cache" not in _post(client, world, headers={"X-Platia-Cache": "no"}).headers
    streamed = _post(client, world, stream=True)
    assert streamed.status_code == 200 and "X-Platia-Cache" not in streamed.headers
    assert len(upstream.requests) == 4


def test_solo_los_propositos_elegidos(client, world, upstream, engine):
    _cache(engine, world, purposes=["proposal_writer"])
    _post(client, world)
    assert "X-Platia-Cache" not in _post(client, world).headers
    writer = {"X-Platia-Purpose": "proposal_writer"}
    _post(client, world, headers=writer)
    assert _post(client, world, headers=writer).headers["X-Platia-Cache"] == "hit"
    assert len(upstream.requests) == 3


def test_respuesta_cortada_no_se_guarda(client, world, upstream, engine):
    _cache(engine, world)
    upstream.replies["anthropic.test"] = httpx.Response(200, json={**ANTHROPIC_REPLY, "stop_reason": "max_tokens"})
    _post(client, world)
    assert _post(client, world).headers["X-Platia-Cache"] == "miss"
    assert len(upstream.requests) == 2


def test_contrato_openai(client, world, upstream, engine):
    _cache(engine, world)
    first = _post(client, world, contract="openai")
    second = _post(client, world, contract="openai")
    assert second.headers["X-Platia-Cache"] == "hit" and second.json() == first.json()
    upstream.replies["openai.test"] = httpx.Response(200, json={
        **OPENAI_REPLY, "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"},
                                     "finish_reason": "length"}]})
    _post(client, world, "otra", contract="openai")
    assert _post(client, world, "otra", contract="openai").headers["X-Platia-Cache"] == "miss"


def test_el_filtro_de_salida_se_aplica_en_cada_acierto(client, world, upstream, engine):
    _cache(engine, world)
    upstream.replies["anthropic.test"] = httpx.Response(200, json={
        **ANTHROPIC_REPLY, "content": [{"type": "text", "text": f"Escribe a {EMAIL}"}]})
    assert _post(client, world).json()["content"][0]["text"] == f"Escribe a {EMAIL}"
    with Session(engine) as db:  # la política cambia después de guardar la respuesta
        db.add(ContentPolicy(scope=PolicyScope.ORGANIZATION, scope_id=world.org_id, direction="output",
                             check_name="pii", action="redact"))
        db.commit()
    hit = _post(client, world)
    assert hit.headers["X-Platia-Cache"] == "hit"
    assert hit.json()["content"][0]["text"] == "Escribe a [OCULTO:email]"
    assert _audit(engine)[-1].security_flags[0]["check"] == "pii"


def test_lo_que_ya_no_se_permite_no_sale_de_la_cache(client, world, engine):
    _cache(engine, world)
    _post(client, world)
    with Session(engine) as db:
        db.execute(update(Application).where(Application.id == world.platia_id).values(allowed_models=["gpt-test"]))
        db.commit()
    assert _post(client, world).status_code == 403


def test_un_acierto_no_gasta_cuota(client, world, upstream, engine):
    _cache(engine, world)
    with Session(engine) as db:
        db.add(UsageLimit(scope=LimitScope.APPLICATION, scope_id=world.platia_id, metric=LimitMetric.REQUESTS,
                          period=LimitPeriod.MINUTE, limit_value=Decimal(1)))
        db.commit()
    assert _post(client, world).status_code == 200
    assert _post(client, world).headers["X-Platia-Cache"] == "hit"
    assert _post(client, world, "otra").status_code == 429


def test_sin_redis_se_sigue_sin_cache(client, world, upstream, engine, monkeypatch, redis_fake):
    _cache(engine, world)

    async def broken(*args, **kwargs):
        raise ConnectionError("redis caído")

    monkeypatch.setattr(redis_fake, "get", broken)
    first = _post(client, world)
    assert first.status_code == 200 and first.headers["X-Platia-Cache"] == "miss"
    monkeypatch.undo()
    monkeypatch.setattr(redis_fake, "set", broken)
    _cache(engine, world)
    assert _post(client, world, "otra").status_code == 200
    assert _post(client, world, "otra").headers["X-Platia-Cache"] == "miss"


def test_valor_ilegible_en_redis_es_un_fallo(client, world, upstream, engine, redis_fake):
    _cache(engine, world)
    _post(client, world)
    import asyncio

    async def spoil():
        async for key in redis_fake.scan_iter(match="gw:cache:*"):
            await redis_fake.set(key, "enc:v1:basura")

    asyncio.run(spoil())
    assert _post(client, world).headers["X-Platia-Cache"] == "miss"


def test_respuesta_demasiado_grande_no_se_guarda(client, world, upstream, engine):
    _cache(engine, world)
    big = "x" * (600 * 1024)
    upstream.replies["anthropic.test"] = httpx.Response(200, json={
        **ANTHROPIC_REPLY, "content": [{"type": "text", "text": big}]})
    _post(client, world)
    assert _post(client, world).headers["X-Platia-Cache"] == "miss"


def test_cache_por_cli(client, world, engine, capsys, monkeypatch):
    settings = client.app.state.settings

    def run(*argv):
        code = cli.main(list(argv), settings=settings)
        out = capsys.readouterr()
        return code, out.out + out.err

    code, out = run("set-cache", "--org", "cotecnia", "--app", "platia", "--ttl", "3600", "--purpose", "resumen")
    assert code == 0 and "3600 s para resumen" in out
    with Session(engine) as db:
        app = db.get(Application, world.platia_id)
        assert (app.cache_ttl_seconds, app.cache_purposes) == (3600, ["resumen"])
    assert run("set-cache", "--org", "cotecnia", "--app", "platia", "--ttl", "-1")[0] == 1
    code, out = run("set-cache", "--org", "cotecnia", "--app", "platia", "--ttl", "0")
    assert code == 0 and "sin caché" in out

    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server)
    for app_id in (world.platia_id, world.platia_id, 999):
        fake.set(f"gw:cache:{world.org_id}:{app_id}:{app_id}{len(fake.keys())}", "x")
    fake.set("gw:cache:999:1:a", "x")
    monkeypatch.setattr("redis.Redis", lambda **kwargs: fakeredis.FakeRedis(server=server))
    code, out = run("cache-clear", "--org", "cotecnia", "--app", "platia")
    assert code == 0 and "borradas de la caché: 2" in out
    code, out = run("cache-clear", "--org", "cotecnia")
    assert "borradas de la caché: 1" in out
    assert fake.keys() == [b"gw:cache:999:1:a"]


def test_patron_de_borrado():
    assert cache.pattern(3) == "gw:cache:3:*:*"
    assert cache.pattern(3, 5) == "gw:cache:3:5:*"
