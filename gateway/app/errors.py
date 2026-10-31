"""Errores del gateway y su forma en cada contrato (ADR-002).

Cada error lleva un código HTTP, un tipo y un mensaje pensado para el cliente. El
mensaje nunca lleva detalles internos (rutas, SQL, claves, respuestas del proveedor sin
limpiar): esos van al log con el `request_id`.

Los tipos son los de la API de Anthropic, que los SDK de los dos contratos entienden.
"""
from fastapi import Request
from fastapi.responses import JSONResponse

from .models import Contract


class GatewayError(Exception):
    status: int = 500
    kind: str = "api_error"

    def __init__(self, message: str, *, status: int | None = None, kind: str | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if kind is not None:
            self.kind = kind


class InvalidRequest(GatewayError):
    status, kind = 400, "invalid_request_error"


class AuthenticationFailed(GatewayError):
    status, kind = 401, "authentication_error"


class PermissionDenied(GatewayError):
    status, kind = 403, "permission_error"


class NotFound(GatewayError):
    status, kind = 404, "not_found_error"


class RequestTooLarge(GatewayError):
    status, kind = 413, "request_too_large"


class UpstreamFailed(GatewayError):
    """El proveedor falló o no se pudo contactar (502)."""
    status, kind = 502, "api_error"


class Unavailable(GatewayError):
    """No hay destino disponible o el proveedor está saturado (503): el cliente puede reintentar."""
    status, kind = 503, "overloaded_error"


class UpstreamTimeout(GatewayError):
    status, kind = 504, "api_error"


class InternalError(GatewayError):
    status, kind = 500, "api_error"


def error_body(contract: Contract, error: GatewayError, request_id: str) -> dict:
    if contract == Contract.ANTHROPIC:
        return {"type": "error", "error": {"type": error.kind, "message": error.message}, "request_id": request_id}
    return {"error": {"message": error.message, "type": error.kind, "param": None, "code": None}}


def error_response(contract: Contract, error: GatewayError, request_id: str) -> JSONResponse:
    return JSONResponse(error_body(contract, error, request_id), status_code=error.status,
                        headers={"X-Request-ID": request_id})


def contract_for(request: Request) -> Contract:
    """Contrato de una petición por su ruta; `/v1/models` depende de quién pregunta."""
    if request.url.path.startswith("/v1/messages"):
        return Contract.ANTHROPIC
    if request.url.path.startswith("/v1/models") and "anthropic-version" in request.headers:
        return Contract.ANTHROPIC
    return Contract.OPENAI
