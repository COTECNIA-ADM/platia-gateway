"""Comprobaciones de salud.

- `/health`: el proceso responde (liveness). No toca dependencias; es lo único que
  Nginx publica.
- `/health/ready`: MySQL y Redis responden (readiness). Lo usa el healthcheck de
  Docker. Solo dice `ok` o `error` por dependencia: el detalle del fallo va al log,
  nunca a la respuesta.
"""
import asyncio
from collections.abc import Awaitable, Callable

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from . import __version__

log = structlog.get_logger(__name__)

Check = Callable[[], Awaitable[None]]

router = APIRouter(tags=["salud"])


async def ping_mysql(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def ping_redis(client: Redis) -> None:
    if not await client.ping():
        raise RuntimeError("Redis no respondió PONG")


async def run_checks(checks: dict[str, Check], timeout: float) -> dict[str, str]:
    async def one(name: str, check: Check) -> tuple[str, str]:
        try:
            await asyncio.wait_for(check(), timeout=timeout)
            return name, "ok"
        except Exception as exc:  # cualquier fallo de una dependencia es "error"
            log.warning("health.check_failed", check=name, error_type=type(exc).__name__, error=str(exc))
            return name, "error"

    results = await asyncio.gather(*(one(name, check) for name, check in checks.items()))
    return dict(results)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "platia-gateway", "version": __version__}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    results = await run_checks(request.app.state.checks, request.app.state.settings.health_timeout_seconds)
    healthy = all(status == "ok" for status in results.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "error", "checks": results},
    )
