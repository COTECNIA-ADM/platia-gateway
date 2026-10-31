import asyncio

from fastapi.testclient import TestClient

from app.health import ping_mysql, ping_redis, run_checks
from app.main import create_app
from app.settings import Settings


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise RuntimeError("contraseña=secreta host=mysql:3306")


async def _hang() -> None:
    await asyncio.sleep(10)


def _client(checks, **settings) -> TestClient:
    return TestClient(create_app(Settings(**settings), checks=checks))


def test_liveness_no_toca_dependencias():
    with _client({"mysql": _boom, "redis": _boom}) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "platia-gateway"


def test_ready_con_todo_bien():
    with _client({"mysql": _ok, "redis": _ok}) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"mysql": "ok", "redis": "ok"}}


def test_ready_con_una_dependencia_caida_da_503_sin_detalles():
    with _client({"mysql": _boom, "redis": _ok}) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "checks": {"mysql": "error", "redis": "ok"}}
    assert "secreta" not in response.text


def test_ready_con_dependencia_colgada_respeta_el_timeout():
    with _client({"mysql": _hang, "redis": _ok}, health_timeout_seconds=0.05) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["mysql"] == "error"


def test_ready_con_dependencias_reales_inalcanzables():
    # Sin `checks`: el arranque crea motor y cliente reales, que no conectan hasta usarse.
    # Apuntan a un puerto cerrado, así que la comprobación falla rápido y da 503.
    settings = Settings(db_host="127.0.0.1", db_port=1, redis_host="127.0.0.1", redis_port=1,
                        health_timeout_seconds=1)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"mysql": "error", "redis": "error"}


def test_documentacion_oculta_en_produccion():
    with _client({}, env="production") as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
    with _client({}, env="development") as client:
        assert client.get("/openapi.json").status_code == 200


async def test_run_checks_ejecuta_en_paralelo():
    async def slow() -> None:
        await asyncio.sleep(0.2)

    loop = asyncio.get_running_loop()
    start = loop.time()
    results = await run_checks({"a": slow, "b": slow, "c": slow}, timeout=1)
    assert results == {"a": "ok", "b": "ok", "c": "ok"}
    assert loop.time() - start < 0.5


class _FakeConn:
    def __init__(self):
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.executed.append(str(statement))


class _FakeEngine:
    def __init__(self):
        self.conn = _FakeConn()

    def connect(self):
        return self.conn


class _FakeRedis:
    def __init__(self, answer):
        self.answer = answer

    async def ping(self):
        return self.answer


async def test_ping_mysql_lanza_select_1():
    engine = _FakeEngine()
    await ping_mysql(engine)
    assert engine.conn.executed == ["SELECT 1"]


async def test_ping_redis():
    await ping_redis(_FakeRedis(True))
    try:
        await ping_redis(_FakeRedis(False))
    except RuntimeError:
        pass
    else:
        raise AssertionError("debía fallar sin PONG")
