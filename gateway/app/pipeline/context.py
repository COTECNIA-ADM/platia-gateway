"""Contexto que comparten las etapas del pipeline y dependencias que reciben."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from time import perf_counter

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts import Usage
from ..errors import GatewayError
from ..models import Contract
from ..providers import Target
from ..settings import Settings


@dataclass(frozen=True)
class Deps:
    """Lo que el pipeline necesita del exterior; en los tests se sustituye."""
    settings: Settings
    sessions: Callable[[], AsyncSession]
    http: httpx.AsyncClient
    encryption_key: bytes | None
    # Enfriamiento de despliegues (G6) y cuotas (G7); None en los tests que no lo necesitan
    redis: Any = None


@dataclass(frozen=True)
class Identity:
    """Quién llama, resuelto por `auth` a partir de la clave."""
    organization_id: int
    team_id: int
    application_id: int
    application_slug: str
    api_key_id: int
    api_key_prefix: str
    trusted: bool
    allowed_purposes: tuple[str, ...]
    allowed_models: tuple[str, ...] | None
    allowed_providers: tuple[str, ...] | None
    # Sal de la organización para las huellas de la auditoría (G9); nunca sale del gateway
    audit_salt: str = field(default="", repr=False)
    # Clave personal (G10): la persona dueña de la clave. Con clave de aplicación, None
    user_id: int | None = None
    # Caché exacta de la aplicación (G11): segundos (0 = sin caché) y propósitos (None = todos)
    cache_ttl_seconds: int = 0
    cache_purposes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ModelInfo:
    id: int
    key: str
    max_output_tokens: int | None
    # Cadena de reserva del modelo (G6), en orden
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """Un destino que puede atender la petición, con el cuerpo ya preparado para él (G6)."""
    model: ModelInfo
    target: Target
    body: dict
    # El gateway pidió el uso al proveedor (OpenAI) sin que el cliente lo pidiera: no se le reenvía
    hide_usage_chunk: bool = False


@dataclass
class PipelineContext:
    request_id: str
    contract: Contract
    headers: Mapping[str, str]
    started: float = field(default_factory=perf_counter)
    body: dict = field(default_factory=dict)
    stream: bool = False
    requested_model: str | None = None
    # auth
    identity: Identity | None = None
    purpose: str = "default"
    end_user_ref: str | None = None
    # policy (el modelo pedido) y execute (el que atiende, si hubo reserva)
    model: ModelInfo | None = None
    # security_input (G8): acción por (sentido, categoría) y lo encontrado, sin el texto
    content_actions: dict = field(default_factory=dict)
    security_flags: list[dict] = field(default_factory=list)
    # quota (G7): contadores admitidos, para ajustarlos al terminar (`quota.settle`)
    quota: Any = None
    # route: destinos en el orden en que se prueban
    candidates: list[Candidate] = field(default_factory=list)
    # execute: el destino del intento en curso (o del último) y cuántos se han probado
    target: Target | None = None
    upstream_body: dict | None = None
    hide_usage_chunk: bool = False
    attempts: int = 0
    # Tiempo esperando al proveedor (todos los intentos) y tiempo propio del gateway hasta
    # tener la respuesta lista (G12, RNF-06)
    upstream_seconds: float = 0.0
    overhead_seconds: float | None = None
    # cache (G11): off, miss o hit; la clave solo si se puede guardar la respuesta
    cache_state: str = "off"
    cache_key: str | None = None
    cache_hit: bool = False
    # execute
    response_body: dict | None = None
    usage: Usage = field(default_factory=Usage)
    # audit
    audit_id: int | None = None
    cost_usd: Decimal = Decimal("0")
    error: GatewayError | None = None
    finished: bool = False
    # telemetría (G12): las métricas de la petición ya se han registrado
    observed: bool = False

    def elapsed_ms(self) -> int:
        return int((perf_counter() - self.started) * 1000)

    def mark_ready(self) -> None:
        """La respuesta está lista para salir (o, en streaming, para empezar a salir): lo
        que ha pasado desde el principio sin contar al proveedor es tiempo del gateway."""
        self.overhead_seconds = max(0.0, perf_counter() - self.started - self.upstream_seconds)

    @property
    def overhead_ms(self) -> int | None:
        return None if self.overhead_seconds is None else round(self.overhead_seconds * 1000)
