import httpx
import pytest

from app import providers
from app.errors import InvalidRequest, Unavailable, UpstreamFailed, UpstreamTimeout
from app.models import ProviderKind

ANTHROPIC = providers.Target(deployment_id=1, provider_slug="anthropic", kind=ProviderKind.ANTHROPIC,
                             base_url=None, upstream_model="claude", api_key="sk-ant-secreto")
OPENAI = providers.Target(deployment_id=2, provider_slug="openai", kind=ProviderKind.OPENAI_COMPATIBLE,
                          base_url="https://llm.interno/v1/", upstream_model="gpt", api_key=None)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_anthropic_url_cabeceras_y_cuerpo():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"id": "msg_1"})

    async with _client(handler) as client:
        data = await providers.send(client, ANTHROPIC, {"model": "claude"}, {
            "anthropic-beta": "x-2026", "authorization": "Bearer plgw-app-a-b", "x-platia-purpose": "p"})
    request = seen["request"]
    assert data == {"id": "msg_1"}
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-secreto"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["anthropic-beta"] == "x-2026"
    # Nada del cliente que no toque: ni su clave ni las cabeceras propias
    assert "authorization" not in request.headers and "x-platia-purpose" not in request.headers


async def test_openai_compatible_sin_clave():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"choices": []})

    async with _client(handler) as client:
        await providers.send(client, OPENAI, {"model": "gpt"}, {})
    assert str(seen["request"].url) == "https://llm.interno/v1/chat/completions"
    assert "authorization" not in seen["request"].headers
    with_key = providers.Target(**{**OPENAI.__dict__, "api_key": "k-1"})
    async with _client(handler) as client:
        await providers.send(client, with_key, {"model": "gpt"}, {})
    assert seen["request"].headers["authorization"] == "Bearer k-1"


def test_la_clave_no_sale_en_repr():
    assert "sk-ant" not in repr(ANTHROPIC)


@pytest.mark.parametrize("status, body, error, status_out", [
    (400, {"error": {"message": "max_tokens: Field required"}}, InvalidRequest, 400),
    (422, {"error": {"message": "x"}}, InvalidRequest, 400),
    (413, {}, InvalidRequest, 413),
    (429, {}, Unavailable, 503),
    (529, {}, Unavailable, 503),
    (401, {"error": {"message": "invalid x-api-key"}}, UpstreamFailed, 502),
    (404, {}, UpstreamFailed, 502),
    (500, "no es json", UpstreamFailed, 502),
])
async def test_errores_del_proveedor(status, body, error, status_out):
    def handler(request):
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    async with _client(handler) as client:
        with pytest.raises(error) as exc:
            await providers.send(client, ANTHROPIC, {}, {})
    assert exc.value.status == status_out
    assert "invalid x-api-key" not in exc.value.message  # los errores de credencial no se reenvían


async def test_error_400_se_reenvia_limpio():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "clave sk-ant-api03-abcdefghijklmnopqrstuvwxyz mala"}})

    async with _client(handler) as client:
        with pytest.raises(InvalidRequest) as exc:
            await providers.send(client, ANTHROPIC, {}, {})
    assert "[REDACTED]" in exc.value.message and "abcdefghijkl" not in exc.value.message


async def test_tiempo_agotado_y_sin_conexion():
    def timeout(request):
        raise httpx.ReadTimeout("lento", request=request)

    def refused(request):
        raise httpx.ConnectError("rechazada", request=request)

    async with _client(timeout) as client:
        with pytest.raises(UpstreamTimeout):
            await providers.send(client, ANTHROPIC, {}, {})
    async with _client(refused) as client:
        with pytest.raises(UpstreamFailed, match="contactar"):
            await providers.send(client, ANTHROPIC, {}, {})


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>"), httpx.Response(200, json=[1, 2])])
async def test_respuesta_200_inesperada(response):
    async with _client(lambda request: response) as client:
        with pytest.raises(UpstreamFailed):
            await providers.send(client, ANTHROPIC, {}, {})


def test_sanitize():
    text = "Bearer abcdefghijklmnop plgw-app-platia-0123456789abcdef api_key=1234567890abc " + "a" * 40
    clean = providers.sanitize(text)
    assert clean.count("[REDACTED]") == 4
    assert len(providers.sanitize("una frase normal " * 100)) == 300
