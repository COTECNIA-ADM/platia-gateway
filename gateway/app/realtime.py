"""Eventos en tiempo real por WebSocket para el panel (G12, cap. 24, ADR-016).

`/events/stream` reenvía los eventos del canal de Redis a los administradores conectados:
los de su organización y los del propio gateway (despliegues). Lo usará el panel (G13).

- **Identidad antes de aceptar.** El navegador no puede poner cabeceras en un WebSocket, así
  que el token de acceso (G10) viaja como subprotocolo: `Sec-WebSocket-Protocol: platia.v1,
  bearer.<token>`. Nunca en la URL, que acaba en los logs. Sin token válido, de un
  administrador con la sesión completa (MFA hecho), la conexión se cierra sin aceptarla.
- **Revocar surte efecto.** Cada `REALTIME_RECHECK_SECONDS` se vuelve a comprobar la sesión en
  MySQL; si se ha cerrado o el token ha caducado, se cierra con 4401 y el panel se vuelve a
  conectar con un token nuevo.
- Como no hay cookies de por medio, otra web no puede abrir la conexión en nombre de nadie.
- Sin Redis no hay eventos: se cierra con 1013 (vuelve a intentarlo más tarde).

La forma (autenticar antes de aceptar, cerrar con 4401) sigue
machinebooks.ai/ES/aigateway/cap-24/block-08.py (MIT).
"""
import json
import time

import anyio
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import identity
from .errors import GatewayError
from .events import CHANNEL
from .models import UserRole

log = structlog.get_logger(__name__)

router = APIRouter()
SUBPROTOCOL = "platia.v1"
_BEARER = "bearer."
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_TRY_LATER = 1013


def _token(websocket: WebSocket) -> tuple[str | None, bool]:
    offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    token = next((p[len(_BEARER):] for p in offered if p.startswith(_BEARER)), None)
    return token, SUBPROTOCOL in offered


def visible(envelope: dict, organization_id: int) -> bool:
    return envelope.get("organization_id") in (None, organization_id)


@router.websocket("/events/stream")
async def stream(websocket: WebSocket) -> None:
    deps = websocket.app.state.deps
    token, versioned = _token(websocket)
    try:
        who = await identity.authenticate(deps, token)
    except GatewayError:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    except Exception as exc:  # noqa: BLE001 - MySQL caído: no se puede comprobar, no se acepta
        log.warning("realtime.auth_unavailable", error_type=type(exc).__name__)
        await websocket.close(code=CLOSE_TRY_LATER)
        return
    if who.role != UserRole.ADMIN or not versioned:
        await websocket.close(code=CLOSE_FORBIDDEN)
        return
    if deps.redis is None:
        await websocket.close(code=CLOSE_TRY_LATER)
        return
    pubsub = deps.redis.pubsub()
    try:
        await pubsub.subscribe(CHANNEL)
    except Exception as exc:  # noqa: BLE001 - sin Redis no hay eventos
        log.warning("realtime.unavailable", error_type=type(exc).__name__)
        await websocket.close(code=CLOSE_TRY_LATER)
        return
    await websocket.accept(subprotocol=SUBPROTOCOL)
    log.info("realtime.connected", user_id=who.user_id, organization_id=who.organization_id)
    reason = "client"
    try:
        async with anyio.create_task_group() as tasks:
            async def listen_client() -> None:
                # El cliente no manda nada; leer sirve para enterarse de que se ha ido
                try:
                    while True:
                        await websocket.receive_text()
                except WebSocketDisconnect:
                    pass
                tasks.cancel_scope.cancel()

            tasks.start_soon(listen_client)
            next_check = time.monotonic() + deps.settings.realtime_recheck_seconds
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None and message.get("type") == "message":
                    data = message["data"]
                    envelope = json.loads(data.decode() if isinstance(data, bytes) else data)
                    if visible(envelope, who.organization_id):
                        await websocket.send_text(json.dumps(envelope, ensure_ascii=False))
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + deps.settings.realtime_recheck_seconds
                    try:
                        await identity.authenticate(deps, token)
                    except Exception:  # noqa: BLE001 - sesión cerrada, token caducado o MySQL caído
                        reason = "session"
                        await websocket.close(code=CLOSE_UNAUTHORIZED)
                        tasks.cancel_scope.cancel()
                        return
    except Exception as exc:  # noqa: BLE001 - Redis o el cliente se han ido
        reason = type(exc).__name__
        log.warning("realtime.stopped", user_id=who.user_id, error_type=reason)
        try:
            await websocket.close(code=CLOSE_TRY_LATER)
        except Exception:  # noqa: BLE001,S110 - ya estaba cerrado
            pass
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await pubsub.unsubscribe(CHANNEL)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001,S110
                pass
        log.info("realtime.disconnected", user_id=who.user_id, reason=reason)
