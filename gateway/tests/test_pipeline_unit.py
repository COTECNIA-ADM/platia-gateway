"""Piezas del pipeline que no necesitan base de datos."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.contracts import Usage
from app.errors import AuthenticationFailed, InvalidRequest, NotFound, error_body
from app.main import create_app
from app.models import Contract, ModelPrice
from app.pipeline import auth
from app.pipeline.audit import compute_cost
from app.pipeline.route import order, provider_key
from app.settings import Settings


def test_clave_en_bearer_o_en_x_api_key():
    assert auth.extract_key({"authorization": "Bearer plgw-app-a"}) == "plgw-app-a"
    assert auth.extract_key({"authorization": "bearer plgw-app-a "}) == "plgw-app-a"
    assert auth.extract_key({"x-api-key": "plgw-app-b"}) == "plgw-app-b"
    assert auth.extract_key({"authorization": "Bearer k", "x-api-key": "k"}) == "k"
    assert auth.extract_key({"authorization": "Basic abc"}) is None
    assert auth.extract_key({}) is None
    with pytest.raises(AuthenticationFailed, match="dos claves"):
        auth.extract_key({"authorization": "Bearer a", "x-api-key": "b"})


async def test_clave_con_otro_formato_no_toca_la_base():
    deps = SimpleNamespace(sessions=None)  # si se usara, fallaría
    for raw in ("sk-ant-123", "plgw-otro-123", "x" * 200):
        with pytest.raises(AuthenticationFailed):
            await auth.resolve(deps, raw)


def test_coste():
    price = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"),
                       cache_read_per_mtok=Decimal("0.3"), cache_write_per_mtok=Decimal("3.75"))
    usage = Usage(input=1000, output=500, cache_read=10_000, cache_write=2000)
    # 1000*3 + 500*15 + 10000*0.3 + 2000*3.75 = 3000 + 7500 + 3000 + 7500 = 21000 → 0.021
    assert compute_cost(usage, price) == Decimal("0.02100000")
    no_cache_price = ModelPrice(input_per_mtok=Decimal("1"), output_per_mtok=Decimal("2"))
    assert compute_cost(Usage(input=1, cache_read=1, cache_write=1), no_cache_price) == Decimal("0.00000300")
    assert compute_cost(usage, None) == Decimal("0")


def _d(id_, priority, weight):
    return SimpleNamespace(id=id_, priority=priority, weight=weight)


def test_orden_por_prioridad_y_peso():
    deployments = [_d(1, 1, 100), _d(2, 0, 0), _d(3, 0, 100), _d(4, 0, 300)]
    firsts = set()
    for _ in range(60):
        ids = [d.id for d in order(deployments)]
        # Prioridad 0 antes que 1; dentro de la 0, el de peso 0 al final (solo como reserva)
        assert ids[2:] == [2, 1] and set(ids[:2]) == {3, 4}
        firsts.add(ids[0])
    assert firsts == {3, 4}  # el peso reparte, pero los dos salen primeros alguna vez
    # Todos con peso 0: en el orden recibido
    assert [d.id for d in order([_d(5, 0, 0), _d(6, 0, 0)])] == [5, 6]


def test_proveedor_sin_clave_o_sin_clave_de_cifrado():
    deps = SimpleNamespace(encryption_key=None)
    assert provider_key(deps, SimpleNamespace(api_key_encrypted=None, slug="local")) is None
    from app.errors import UpstreamFailed
    with pytest.raises(UpstreamFailed):
        provider_key(deps, SimpleNamespace(api_key_encrypted="enc:v1:x", slug="a"))


def test_formato_de_error_por_contrato():
    assert error_body(Contract.ANTHROPIC, NotFound("No"), "r") == {
        "type": "error", "error": {"type": "not_found_error", "message": "No"}, "request_id": "r"}
    assert error_body(Contract.OPENAI, InvalidRequest("Mal"), "r") == {
        "error": {"message": "Mal", "type": "invalid_request_error", "param": None, "code": None}}


def _client(**settings) -> TestClient:
    async def ok():
        return None
    return TestClient(create_app(Settings(**settings), checks={"mysql": ok}))


def test_sin_clave_401_en_el_formato_de_cada_contrato():
    with _client() as client:
        anthropic = client.post("/v1/messages", json={})
        openai = client.post("/v1/chat/completions", json={}, headers={"X-Request-ID": "req-12345678"})
        models = client.get("/v1/models", headers={"anthropic-version": "2023-06-01"})
    assert anthropic.status_code == 401 and anthropic.json()["error"]["type"] == "authentication_error"
    assert anthropic.json()["type"] == "error" and anthropic.headers["X-Request-ID"]
    assert openai.status_code == 401 and openai.json()["error"]["type"] == "authentication_error"
    assert openai.headers["X-Request-ID"] == "req-12345678"
    assert models.status_code == 401 and models.json()["type"] == "error"


def test_clave_de_cifrado_mal_configurada_impide_arrancar():
    from app.crypto import CryptoError
    with pytest.raises(CryptoError):
        create_app(Settings(encryption_key="corta"))
