"""Tareas de fondo del gateway: sellar la auditoría (G9) y entregar los webhooks (G12).

En desarrollo y en los tests corren dentro del proceso de la API. En el compose las corre
un proceso aparte (`python -m app.worker`, G15, ADR-019): así un reinicio de la API no
corta una entrega a medias y el trabajo no se repite en cada proceso de uvicorn. Las dos son
seguras con varios procesos a la vez (bloqueo de fila y `SKIP LOCKED`).
"""
import asyncio
from contextlib import suppress

import httpx
import structlog

from . import audit_chain, webhooks
from .pipeline.context import Deps

log = structlog.get_logger(__name__)


async def seal_forever(deps: Deps) -> None:
    """Sella la auditoría cada `AUDIT_SEAL_INTERVAL_SECONDS` (G9). Un fallo no para nada:
    va al log y se reintenta en la vuelta siguiente."""
    settings = deps.settings
    while True:
        try:
            while await audit_chain.seal(deps.sessions, settings.audit_pending_max_seconds) >= audit_chain.SEAL_BATCH:
                pass  # quedan más: seguir sin esperar
        except Exception as exc:  # noqa: BLE001 - cualquier fallo se registra y se reintenta
            log.error("audit.seal_failed", error_type=type(exc).__name__)
        await asyncio.sleep(settings.audit_seal_interval_seconds)


def start(deps: Deps, hooks_http: httpx.AsyncClient) -> list[asyncio.Task]:
    """Arranca las tareas que la configuración tiene activadas."""
    settings = deps.settings
    tasks = []
    if settings.audit_seal_interval_seconds > 0:
        tasks.append(asyncio.create_task(seal_forever(deps), name="sellado"))
    if settings.webhook_poll_seconds > 0:
        tasks.append(asyncio.create_task(webhooks.deliver_forever(deps, hooks_http), name="webhooks"))
    return tasks


async def stop(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task
