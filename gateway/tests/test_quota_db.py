"""Cuotas y límites de ritmo (G7): MySQL real, Redis en memoria y proveedores simulados.

En `world` (conftest.py), cada respuesta de claude-test gasta 1000 tokens de entrada, 500 de
salida y 2000 de caché leída: 0,0111 USD con su precio (3 / 15 / 0,3 USD por Mtok).
"""
import asyncio
import dataclasses
import time
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest

from app.models import AuditLog, LimitMetric, LimitPeriod, LimitScope, UsageLimit
from app.pipeline import quota

from .conftest import needs_db

pytestmark = needs_db
COST = Decimal("0.0111")



@pytest.fixture(autouse=True)
def _misma_ventana_de_minuto():
    """Las pruebas de ventana por minuto fallarían si cruzaran el cambio de minuto a mitad."""
    second = time.time() % 60
    if second > 54:
        time.sleep(60.5 - second)


def _post(client, world, *, user=None, stream=False, contract="anthropic"):
    headers = {"x-api-key": world.keys["platia"]}
    if user:
        headers["X-Platia-User-Id"] = user
    body = {"model": "claude-test", "max_tokens": 100, "messages": [{"role": "user", "content": "Hola"}]}
    if stream:
        body["stream"] = True
    path = "/v1/messages" if contract == "anthropic" else "/v1/chat/completions"
    return client.post(path, json=body, headers=headers)


def _limit(engine, world, metric, period, value, *, scope=LimitScope.APPLICATION, scope_id=None, subject=None,
           alert_pct=80) -> int:
    ids = {LimitScope.ORGANIZATION: world.org_id, LimitScope.APPLICATION: world.platia_id,
           LimitScope.END_USER: world.platia_id}
    with Session(engine) as db:
        row = UsageLimit(scope=scope, scope_id=scope_id if scope_id is not None else ids[scope], subject=subject,
                         metric=metric, period=period, limit_value=Decimal(str(value)), alert_pct=alert_pct)
        db.add(row)
        db.commit()
        return row.id


def _audit(engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def _redis(redis, coro_name, *args):
    return asyncio.run(getattr(redis, coro_name)(*args))


def _keys(redis, pattern="gw:q:*") -> dict[str, int]:
    async def read():
        keys = sorted(k.decode() for k in await redis.keys(pattern))
        return {k: int(await redis.get(k)) for k in keys if not k.split(":")[-2] == "alert"}
    return asyncio.run(read())


class Recorder:
    """Sustituye el log de la etapa: guarda cada evento con sus campos."""

    def __init__(self):
        self.events: list[dict] = []

    def _record(self, event, **fields):
        self.events.append({"event": event, **fields})

    info = warning = error = _record

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]


@pytest.fixture
def logs(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(quota, "log", recorder)
    return recorder


class Broken:
    async def mget(self, *args, **kwargs):
        raise ConnectionError("sin redis")
    set = delete = incr = expire = eval = get = mget


def test_sin_limites_no_se_toca_redis_ni_hay_cabeceras(client, world, redis_fake):
    response = _post(client, world)
    assert response.status_code == 200
    assert "X-Platia-Budget-Remaining-Usd" not in response.headers
    assert "X-Platia-RateLimit-Remaining" not in response.headers
    assert _keys(redis_fake) == {}


def test_presupuesto_diario_agotado_devuelve_402(client, world, upstream, engine):
    _limit(engine, world, LimitMetric.COST_USD, LimitPeriod.DAY, "0.02")
    first = _post(client, world)
    assert first.status_code == 200
    assert first.headers["X-Platia-Budget-Remaining-Usd"] == "0.020000"
    second = _post(client, world)  # gastado 0,0111 < 0,02: pasa, aunque se pase un poco
    assert second.status_code == 200
    assert second.headers["X-Platia-Budget-Remaining-Usd"] == "0.008900"
    third = _post(client, world)
    assert third.status_code == 402
    error = third.json()["error"]
    assert error["type"] == "billing_error"
    assert "gasto diario de la aplicación" in error["message"] and "0.02 USD" in error["message"]
    assert third.headers["X-Platia-Quota-Reset"].endswith("T00:00:00Z")
    assert "Retry-After" not in third.headers
    assert len(upstream.requests) == 2
    rows = _audit(engine)
    assert [r.status for r in rows] == ["success", "success", "blocked"]
    assert (rows[2].http_status, rows[2].error_type) == (402, "billing_error")


def test_peticiones_por_minuto_devuelve_429_con_retry_after(client, world, engine):
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 2, scope=LimitScope.ORGANIZATION)
    first = _post(client, world)
    assert (first.headers["X-Platia-RateLimit-Limit"], first.headers["X-Platia-RateLimit-Remaining"]) == ("2", "1")
    assert _post(client, world).headers["X-Platia-RateLimit-Remaining"] == "0"
    third = _post(client, world, contract="openai")
    assert third.status_code == 429
    assert third.json()["error"]["type"] == "rate_limit_error"
    assert "peticiones por minuto de la organización" in third.json()["error"]["message"]
    assert 1 <= int(third.headers["Retry-After"]) <= 61
    assert _audit(engine)[-1].status == "blocked"


def test_simultaneas_se_liberan_al_terminar(client, world, engine, redis_fake):
    _limit(engine, world, LimitMetric.CONCURRENT, LimitPeriod.NONE, 1)
    assert _post(client, world).status_code == 200
    assert _post(client, world).status_code == 200  # la anterior ya liberó su hueco
    [(key, value)] = _keys(redis_fake).items()
    assert key == f"gw:q:concurrent:application:{world.platia_id}:live" and value == 0
    _redis(redis_fake, "set", key, 1)  # otra petición en vuelo
    busy = _post(client, world)
    assert busy.status_code == 429 and busy.headers["Retry-After"] == "1"


def test_simultaneas_se_liberan_aunque_falle_el_proveedor(client, world, upstream, engine, redis_fake):
    _limit(engine, world, LimitMetric.CONCURRENT, LimitPeriod.NONE, 1)
    upstream.replies["anthropic.test"] = httpx.Response(500)
    assert _post(client, world).status_code == 502
    assert list(_keys(redis_fake).values()) == [0]


def test_tokens_se_corrigen_con_el_uso_real(client, world, engine, redis_fake):
    _limit(engine, world, LimitMetric.TOKENS, LimitPeriod.MINUTE, 100_000)
    _post(client, world)
    assert list(_keys(redis_fake).values()) == [3500]  # 1000 + 500 + 2000, no la estimación


def test_streaming_cuenta_el_gasto_y_libera_al_terminar(client, world, engine, redis_fake):
    _limit(engine, world, LimitMetric.COST_USD, LimitPeriod.MONTH, 10)
    _limit(engine, world, LimitMetric.CONCURRENT, LimitPeriod.NONE, 5)
    response = _post(client, world, stream=True)
    assert response.status_code == 200 and response.headers["X-Platia-Budget-Remaining-Usd"] == "10.000000"
    counters = _keys(redis_fake)
    assert counters[f"gw:q:concurrent:application:{world.platia_id}:live"] == 0
    [cost] = [v for k, v in counters.items() if k.startswith("gw:q:cost_usd")]
    assert cost == 11100  # millonésimas de USD


def test_cuota_por_persona_y_excepcion_de_una_persona(client, world, engine):
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.DAY, 1, scope=LimitScope.END_USER)
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.DAY, 3, scope=LimitScope.END_USER, subject="jefa")
    assert _post(client, world, user="7").status_code == 200
    blocked = _post(client, world, user="7")
    assert blocked.status_code == 402 and "de la persona" in blocked.json()["error"]["message"]
    assert _post(client, world, user="8").status_code == 200  # cada persona tiene su cuota
    assert [_post(client, world, user="jefa").status_code for _ in range(4)] == [200, 200, 200, 402]
    assert _post(client, world).status_code == 200  # sin persona declarada, no aplica


def test_contador_perdido_se_reconstruye_desde_la_auditoria(client, world, engine, logs):
    assert _post(client, world).status_code == 200
    assert _post(client, world).status_code == 200
    _limit(engine, world, LimitMetric.COST_USD, LimitPeriod.DAY, "0.02")  # límite nuevo: Redis no sabe nada
    response = _post(client, world)
    assert response.status_code == 402  # 2 × 0,0111 ya gastados según la auditoría
    assert any(e["event"] == "quota.counter_rebuilt" and e["value"] == 22200 for e in logs.events)


def test_aviso_al_pasar_el_umbral_una_sola_vez(client, world, engine, logs):
    _limit(engine, world, LimitMetric.COST_USD, LimitPeriod.DAY, "0.04", alert_pct=50)
    for _ in range(3):  # 0,0111 (28 %), 0,0222 (56 %), 0,0333 (83 %)
        assert _post(client, world).status_code == 200
    alerts = [e for e in logs.events if e["event"] == "quota.alert"]
    assert len(alerts) == 1 and alerts[0]["threshold_pct"] == 50 and alerts[0]["used"] == "0.0222"
    logs.events.clear()
    _post(client, world)  # 0,0444: agotado
    assert logs.names() == ["quota.exhausted"]


def test_aviso_de_peticiones_al_admitir(client, world, engine, logs):
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MONTH, 4, scope=LimitScope.ORGANIZATION, alert_pct=50)
    _post(client, world)  # 1 de 4
    _post(client, world)  # 2 de 4: aviso
    assert logs.names() == ["quota.counter_rebuilt", "quota.alert"]


def test_redis_caido_con_limites_falla_cerrado(client, world, upstream, engine):
    _limit(engine, world, LimitMetric.COST_USD, LimitPeriod.MONTH, 100)
    deps = client.app.state.deps
    client.app.state.deps = dataclasses.replace(deps, redis=Broken())
    response = _post(client, world)
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "El gateway no puede comprobar las cuotas en este momento"
    assert upstream.requests == []


def test_sin_redis_configurado_y_con_limites_falla_cerrado(client, world, upstream, engine):
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 100)
    deps = client.app.state.deps
    client.app.state.deps = dataclasses.replace(deps, redis=None)
    assert _post(client, world).status_code == 503
    assert upstream.requests == []


def test_redis_caido_sin_limites_sigue_sirviendo(client, world):
    deps = client.app.state.deps
    client.app.state.deps = dataclasses.replace(deps, redis=Broken())
    assert _post(client, world).status_code == 200


def test_fallo_al_ajustar_no_cambia_la_respuesta(client, world, engine, redis_fake, logs):
    _limit(engine, world, LimitMetric.TOKENS, LimitPeriod.MINUTE, 100_000)

    class FailsOnSettle:
        def __init__(self, real):
            self.real, self.calls = real, 0

        def __getattr__(self, name):
            return getattr(self.real, name)

        async def eval(self, *args):
            self.calls += 1
            if self.calls > 1:
                raise ConnectionError("se cayó a mitad")
            return await self.real.eval(*args)

    deps = client.app.state.deps
    client.app.state.deps = dataclasses.replace(deps, redis=FailsOnSettle(redis_fake))
    assert _post(client, world).status_code == 200
    assert logs.names() == ["quota.settle_failed"]


def test_limite_inactivo_o_borrado_no_cuenta(client, world, engine):
    first = _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 0)
    second = _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.DAY, 0)
    assert _post(client, world).status_code == 429
    with Session(engine) as db:
        db.get(UsageLimit, first).is_active = False
        db.get(UsageLimit, second).deleted_at = db.get(UsageLimit, second).created_at
        db.commit()
    assert _post(client, world).status_code == 200


def test_limites_de_otro_equipo_u_organizacion_no_aplican(client, world, engine):
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 0, scope=LimitScope.ORGANIZATION,
           scope_id=world.org_id + 1)
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 0, scope=LimitScope.TEAM, scope_id=9999)
    assert _post(client, world).status_code == 200


def test_dos_limites_del_mismo_contador_manda_el_mas_estricto(client, world, engine, redis_fake):
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 5)
    _limit(engine, world, LimitMetric.REQUESTS, LimitPeriod.MINUTE, 1)
    first = _post(client, world)
    assert first.headers["X-Platia-RateLimit-Limit"] == "1"
    assert _post(client, world).status_code == 429
    assert list(_keys(redis_fake).values()) == [1]  # un solo contador, subido una vez
