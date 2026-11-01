"""Aplicación FastAPI del gateway.

Al arrancar crea las conexiones (MySQL, Redis y el cliente HTTP hacia los proveedores) y
carga la clave de cifrado. Las rutas de inferencia están en `api.py` y el pipeline en
`pipeline/`.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from functools import partial

import httpx
import structlog
from fastapi import FastAPI, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from . import __version__, audit_chain
from .api import router as api_router
from .crypto import load_key
from .errors import InternalError, contract_for, error_response
from .health import Check, ping_mysql, ping_redis
from .health import router as health_router
from .logs import configure_logging
from .pipeline.context import Deps
from .pipeline.runner import request_id_of
from .settings import Settings, get_settings

log = structlog.get_logger(__name__)


async def seal_forever(deps: Deps) -> None:
    """Sella la auditoría cada `AUDIT_SEAL_INTERVAL_SECONDS` (G9). Un fallo no para el gateway:
    va al log y se reintenta en la vuelta siguiente."""
    settings = deps.settings
    while True:
        try:
            while await audit_chain.seal(deps.sessions, settings.audit_pending_max_seconds) >= audit_chain.SEAL_BATCH:
                pass  # quedan más: seguir sin esperar
        except Exception as exc:  # noqa: BLE001 - cualquier fallo se registra y se reintenta
            log.error("audit.seal_failed", error_type=type(exc).__name__)
        await asyncio.sleep(settings.audit_seal_interval_seconds)


def create_app(settings: Settings | None = None, checks: dict[str, Check] | None = None,
               transport: httpx.AsyncBaseTransport | None = None, redis_client=None) -> FastAPI:
    """Crea la aplicación. `checks`, `transport` y `redis_client` sustituyen la salud, los
    proveedores y Redis reales (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    # Una clave mal escrita impide arrancar: mejor que descubrirlo en la primera petición
    encryption_key = load_key(settings.encryption_key) if settings.encryption_key else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Ni el motor ni el cliente conectan al crearse: el arranque no depende de que
        # MySQL o Redis estén listos; /health/ready dice cuándo lo están.
        engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_recycle=3600,
                                     pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
        redis = redis_client if redis_client is not None else Redis(
            host=settings.redis_host, port=settings.redis_port, db=settings.redis_db,
            password=settings.redis_password or None, socket_timeout=settings.health_timeout_seconds,
            socket_connect_timeout=settings.health_timeout_seconds)
        http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(
            settings.provider_timeout_seconds, connect=settings.provider_connect_timeout_seconds))
        app.state.checks = checks if checks is not None else {
            "mysql": partial(ping_mysql, engine), "redis": partial(ping_redis, redis)}
        app.state.deps = Deps(settings=settings, http=http, encryption_key=encryption_key, redis=redis,
                              sessions=async_sessionmaker(engine, expire_on_commit=False))
        if encryption_key is None:
            log.warning("gateway.no_encryption_key", detail="sin ENCRYPTION_KEY no se pueden usar proveedores con clave")
        log.info("gateway.started", version=__version__, env=settings.env)
        sealer = asyncio.create_task(seal_forever(app.state.deps)) if settings.audit_seal_interval_seconds > 0 else None
        try:
            yield
        finally:
            if sealer is not None:
                sealer.cancel()
                with suppress(asyncio.CancelledError):
                    await sealer
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    docs = {} if not settings.is_production else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="PLATIA Gateway", version=__version__, lifespan=lifespan, **docs)
    app.state.settings = settings
    app.include_router(health_router)
    app.include_router(api_router)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        # Último recurso: nunca un detalle interno en la respuesta
        request_id = request_id_of(request)
        log.exception("gateway.unhandled", request_id=request_id, path=request.url.path)
        return error_response(contract_for(request), InternalError("Error interno del gateway"), request_id)

    return app


app = create_app()
