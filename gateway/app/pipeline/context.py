"""Contexto que comparten las etapas del pipeline y dependencias que reciben."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
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


@dataclass(frozen=True)
class ModelInfo:
    id: int
    key: str
    max_output_tokens: int | None


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
    # policy
    model: ModelInfo | None = None
    # route
    target: Target | None = None
    upstream_body: dict | None = None
    # execute
    response_body: dict | None = None
    usage: Usage = field(default_factory=Usage)
    # audit
    audit_id: int | None = None
    cost_usd: Decimal = Decimal("0")
    error: GatewayError | None = None
    finished: bool = False

    def elapsed_ms(self) -> int:
        return int((perf_counter() - self.started) * 1000)
