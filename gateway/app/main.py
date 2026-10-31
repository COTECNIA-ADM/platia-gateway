"""Aplicación FastAPI del gateway.

G0 solo trae el esqueleto: configuración por entorno, logs estructurados y
comprobaciones de salud contra MySQL y Redis. El contrato de inferencia llega en G3.
"""
from contextlib import asynccontextmanager
from functools import partial

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from . import __version__
from .health import Check, ping_mysql, ping_redis
from .health import router as health_router
from .logs import configure_logging
from .settings import Settings, get_settings

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None, checks: dict[str, Check] | None = None) -> FastAPI:
    """Crea la aplicación. `checks` sustituye las comprobaciones reales (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if checks is not None:
            app.state.checks = checks
            yield
            return
        # Ni el motor ni el cliente conectan al crearse: el arranque no depende de que
        # MySQL o Redis estén listos; /health/ready dice cuándo lo están.
        engine = create_async_engine(settings.database_url, pool_pre_ping=True,
                                     pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
        redis = Redis(host=settings.redis_host, port=settings.redis_port, db=settings.redis_db,
                      password=settings.redis_password or None, socket_timeout=settings.health_timeout_seconds)
        app.state.checks = {"mysql": partial(ping_mysql, engine), "redis": partial(ping_redis, redis)}
        log.info("gateway.started", version=__version__, env=settings.env)
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    docs = {} if not settings.is_production else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="PLATIA Gateway", version=__version__, lifespan=lifespan, **docs)
    app.state.settings = settings
    app.include_router(health_router)
    return app


app = create_app()
