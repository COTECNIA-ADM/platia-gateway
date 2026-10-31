"""Adaptadores de proveedor (cap. 7): Anthropic y APIs compatibles con OpenAI.

Cada adaptador sabe a qué URL llamar, cómo autenticarse y cómo leer un error. No conoce
el pipeline: recibe el cuerpo ya preparado y devuelve el JSON del proveedor o un
`GatewayError` con un mensaje que se puede enseñar al cliente.

Las cabeceras propias del gateway y la clave del cliente nunca se reenvían.
"""
import re
from dataclasses import dataclass

import httpx
import structlog

from .contracts import ANTHROPIC_VERSION
from .errors import InvalidRequest, Unavailable, UpstreamFailed, UpstreamTimeout
from .models import ProviderKind

log = structlog.get_logger(__name__)

DEFAULT_BASE_URLS = {ProviderKind.ANTHROPIC: "https://api.anthropic.com"}
# Cabeceras del cliente que Anthropic necesita ver (versión de la API y funciones beta)
_ANTHROPIC_PASSTHROUGH = ("anthropic-version", "anthropic-beta")


@dataclass(frozen=True)
class Target:
    """Destino elegido por `route`: un despliegue con su proveedor."""
    deployment_id: int
    provider_slug: str
    kind: ProviderKind
    base_url: str | None
    upstream_model: str
    api_key: str | None  # descifrada; solo vive en memoria durante la petición

    def __repr__(self) -> str:  # que la clave no salga nunca en un log o una traza
        return f"Target(deployment_id={self.deployment_id}, provider={self.provider_slug!r})"


# Adaptado de machinebooks.ai/ES/aigateway/cap-07/block-03.py (MIT, ver THIRD_PARTY_NOTICES.md):
# un mensaje de error del proveedor puede repetir una clave o un token; se tachan antes de
# reenviarlo al cliente.
_SENSITIVE = re.compile(
    r"(sk-[A-Za-z0-9\-_]{16,}|plgw-[a-z]{3}-[A-Za-z0-9\-_]{8,}|Bearer\s+[A-Za-z0-9\-_.]{10,}"
    r"|api[_\-]?key[\"'\s:=]+[A-Za-z0-9\-_.]{10,}|[A-Za-z0-9]{32,})",
    re.IGNORECASE,
)


def sanitize(message: str) -> str:
    return _SENSITIVE.sub("[REDACTED]", message[:300])


def _url(target: Target) -> str:
    base = (target.base_url or DEFAULT_BASE_URLS.get(target.kind, "")).rstrip("/")
    if target.kind == ProviderKind.ANTHROPIC:
        return f"{base}/v1/messages"
    return f"{base}/chat/completions"


def _headers(target: Target, client_headers, *, stream: bool = False) -> dict[str, str]:
    headers = {"content-type": "application/json", "accept": "text/event-stream" if stream else "application/json"}
    if target.kind == ProviderKind.ANTHROPIC:
        headers["anthropic-version"] = ANTHROPIC_VERSION
        for name in _ANTHROPIC_PASSTHROUGH:
            if client_headers.get(name):
                headers[name] = client_headers[name]
        if target.api_key:
            headers["x-api-key"] = target.api_key
    elif target.api_key:
        headers["authorization"] = f"Bearer {target.api_key}"
    return headers


def _upstream_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except (ValueError, httpx.ResponseNotRead):  # cuerpo que no es JSON o que no se pudo leer
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return ""


def _raise_for(response: httpx.Response, target: Target) -> None:
    status = response.status_code
    message = sanitize(_upstream_message(response))
    log.warning("provider.error", provider=target.provider_slug, deployment_id=target.deployment_id,
                upstream_status=status, upstream_message=message)
    if status in (400, 413, 422):
        # Error de la petición del cliente: se le devuelve, limpio, para que lo corrija
        raise InvalidRequest(f"El proveedor rechazó la petición: {message or 'petición no válida'}",
                             status=413 if status == 413 else 400,
                             kind="request_too_large" if status == 413 else None)
    if status in (429, 503, 529):
        raise Unavailable("El proveedor está saturado; vuelve a intentarlo en unos segundos")
    if status in (401, 403, 404):
        # Credencial o modelo del proveedor mal configurados: es un problema nuestro, no del cliente
        raise UpstreamFailed("El proveedor no aceptó la configuración del gateway")
    raise UpstreamFailed("El proveedor devolvió un error")


def _request_failed(exc: httpx.HTTPError, target: Target) -> UpstreamFailed | UpstreamTimeout:
    if isinstance(exc, httpx.TimeoutException):
        log.warning("provider.timeout", provider=target.provider_slug, error_type=type(exc).__name__)
        return UpstreamTimeout("El proveedor no respondió a tiempo")
    log.warning("provider.unreachable", provider=target.provider_slug, error_type=type(exc).__name__)
    return UpstreamFailed("No se pudo contactar con el proveedor")


async def open_stream(client: httpx.AsyncClient, target: Target, body: dict, client_headers) -> httpx.Response:
    """Abre la respuesta en streaming y comprueba el código antes de enviar nada al cliente.

    Así un error del proveedor (400, 429, 401...) llega al cliente como un error HTTP normal.
    Quien recibe la respuesta abierta debe cerrarla (`aclose`).
    """
    request = client.build_request("POST", _url(target), json=body, headers=_headers(target, client_headers,
                                                                                      stream=True))
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _request_failed(exc, target) from exc
    if response.status_code != 200:
        try:
            await response.aread()
        except httpx.HTTPError:
            pass
        finally:
            await response.aclose()
        _raise_for(response, target)
    return response


async def send(client: httpx.AsyncClient, target: Target, body: dict, client_headers) -> dict:
    """Envía la petición al proveedor y devuelve su JSON."""
    try:
        response = await client.post(_url(target), json=body, headers=_headers(target, client_headers))
    except httpx.HTTPError as exc:
        raise _request_failed(exc, target) from exc
    if response.status_code != 200:
        _raise_for(response, target)
    try:
        data = response.json()
    except ValueError as exc:
        raise UpstreamFailed("El proveedor devolvió una respuesta que no es JSON") from exc
    if not isinstance(data, dict):
        raise UpstreamFailed("El proveedor devolvió una respuesta inesperada")
    return data

