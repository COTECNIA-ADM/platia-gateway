"""Proceso de tareas de fondo (G15, ADR-019): sella la auditoría y entrega los webhooks.

    python -m app.worker            # arranca (servicio `worker` del compose)
    python -m app.worker --check    # healthcheck: el proceso sigue vivo y trabajando

Usa la misma imagen y configuración que la API, que en el compose arranca con
BACKGROUND_TASKS=false. No llama a los proveedores ni publica ningún puerto.
"""
import asyncio
import os
import signal
import sys
import time

import httpx
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import __version__, background, webhooks
from .connections import engine_for, redis_for
from .crypto import load_key
from .logs import configure_logging
from .pipeline.context import Deps
from .settings import Settings, get_settings

log = structlog.get_logger(__name__)
HEARTBEAT_SECONDS = 15
# Sin latido en este tiempo, el healthcheck da el proceso por colgado
HEARTBEAT_MAX_AGE = 120


async def _beat(path: str) -> None:
    while True:
        with open(path, "w") as f:
            f.write(str(int(time.time())))
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def run(settings: Settings, stop: asyncio.Event | None = None,
              webhook_transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Corre hasta que `stop` se activa (SIGTERM de Docker o los tests)."""
    stop = stop or asyncio.Event()
    engine = engine_for(settings, pool_size=2)
    redis = redis_for(settings)
    # El cliente de proveedores no se usa aquí, pero Deps lo pide
    http = httpx.AsyncClient()
    hooks_http = (httpx.AsyncClient(transport=webhook_transport, follow_redirects=False, trust_env=False)
                  if webhook_transport is not None else webhooks.client(settings))
    deps = Deps(settings=settings, http=http, redis=redis,
                encryption_key=load_key(settings.encryption_key) if settings.encryption_key else None,
                sessions=async_sessionmaker(engine, expire_on_commit=False))
    tasks = background.start(deps, hooks_http)
    tasks.append(asyncio.create_task(_beat(settings.worker_heartbeat_file), name="latido"))
    log.info("worker.started", version=__version__, env=settings.env, tasks=[t.get_name() for t in tasks])
    try:
        await stop.wait()
    finally:
        await background.stop(tasks)
        await hooks_http.aclose()
        await http.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("worker.stopped")


def healthy(path: str, now: float | None = None) -> bool:
    try:
        return (now or time.time()) - os.path.getmtime(path) < HEARTBEAT_MAX_AGE
    except OSError:
        return False


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    if argv == ["--check"]:
        return 0 if healthy(settings.worker_heartbeat_file) else 1
    configure_logging(settings.log_level, settings.log_format)
    settings.check_production()
    asyncio.run(_main(settings))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
