"""Webhooks firmados: alta, cola de entregas, firma, protección SSRF y reintentos (G12, cap. 24, ADR-016).

**Firma.** Cada entrega lleva `X-Platia-Signature: t=<segundos unix>,v1=<hex>`, donde el hex
es HMAC-SHA256 con el secreto del webhook sobre `<t>.<cuerpo>`. El receptor recalcula la
firma con el cuerpo tal cual llega, la compara en tiempo constante y rechaza las de más de
5 minutos (así una entrega copiada no se puede reenviar más tarde). `X-Platia-Delivery`
identifica la entrega y el `id` del cuerpo, el evento: con ellos el receptor descarta
duplicados (se entrega al menos una vez).

**SSRF.** Solo `https` y solo direcciones públicas: el nombre se resuelve, se rechaza si
alguna dirección es privada, local, reservada o de multidifusión, y la conexión se hace a
esa misma dirección (con el nombre en `Host` y en SNI, para que el certificado se compruebe
contra él). Así un DNS que cambia entre la comprobación y la conexión no lleva a la red
interna. Nunca se siguen redirecciones. Los receptores internos se permiten uno a uno con
`WEBHOOK_PRIVATE_HOSTS` (y solo ellos pueden usar `http`).

**Entrega.** `emit` deja una fila por webhook en `webhook_deliveries` y el bucle de
`deliver_forever` las envía: reclama un lote con `FOR UPDATE SKIP LOCKED` (varios procesos
no envían la misma), reintenta con esperas crecientes y, tras `WEBHOOK_MAX_ATTEMPTS`, la da
por fallida. Con `DISABLE_AFTER` entregas fallidas seguidas, el webhook se desactiva solo.

Firma, cabeceras y comprobación de URL siguen machinebooks.ai/ES/aigateway/cap-24/block-14.py,
block-17.py y block-18.py (MIT); la marca de tiempo en la firma es nuestra.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from sqlalchemy import delete, or_, select, update

from .crypto import decrypt, encrypt
from .events import WEBHOOK_EVENTS, dumps
from .models import DeliveryStatus, Webhook, WebhookDelivery
from .models.base import utcnow
from .pipeline.context import Deps
from .telemetry import WEBHOOK_DELIVERIES

log = structlog.get_logger(__name__)

SECRET_PREFIX = "plgw-whsec-"
USER_AGENT = "PLATIA-Gateway-Webhook/1"
# Espera antes de cada reintento (el último valor se repite)
BACKOFF_SECONDS = (10, 60, 300, 1800, 7200)
DISABLE_AFTER = 5
BATCH = 20
# Una entrega reclamada no la toca otro proceso durante este margen sobre el tiempo de envío
_LEASE_MARGIN = 60


class WebhookUrlError(ValueError):
    """La URL no se puede usar como destino (formato, esquema o dirección no permitidos)."""


def new_secret() -> str:
    return SECRET_PREFIX + secrets.token_hex(32)


def secret_context(webhook_id: int) -> str:
    return f"webhook:{webhook_id}"


def sign(secret: str, timestamp: int, body: str) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: str, *, tolerance: int = 300, now: float | None = None) -> bool:
    """Lo que hace el receptor (aquí para los tests y como ejemplo en docs/telemetria.md)."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), f"t={timestamp},v1={parts.get('v1', '')}")


# --- URL y SSRF

def check_url(url: str, private_hosts: frozenset[str]) -> tuple[str, str, int]:
    """Esquema, nombre y puerto de una URL de destino válida."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise WebhookUrlError("URL no válida") from exc
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        raise WebhookUrlError("La URL necesita un nombre de servidor y no puede llevar usuario ni contraseña")
    if parts.scheme not in ("https", "http"):
        raise WebhookUrlError("Solo URLs https")
    if parts.scheme == "http" and host not in private_hosts:
        raise WebhookUrlError("Solo URLs https (http solo para los receptores de WEBHOOK_PRIVATE_HOSTS)")
    if len(url) > 2048:
        raise WebhookUrlError("URL demasiado larga")
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def pinned_request(url: str, private_hosts: frozenset[str]) -> tuple[str, dict, dict]:
    """URL con la dirección ya comprobada, cabecera Host y extensión SNI para httpx."""
    scheme, host, port = check_url(url, private_hosts)
    try:
        addresses = [host] if _is_ip(host) else await resolve(host, port)
    except OSError as exc:
        raise WebhookUrlError("El nombre del receptor no se resuelve") from exc
    if not addresses:
        raise WebhookUrlError("El nombre del receptor no se resuelve")
    if host not in private_hosts and not all(_public(a) for a in addresses):
        raise WebhookUrlError("La URL apunta a una dirección interna")
    address = addresses[0]
    parts = urlsplit(url)
    netloc_host = f"[{address}]" if ":" in address else address
    explicit_port = parts.port is not None
    pinned = urlunsplit((scheme, netloc_host + (f":{parts.port}" if explicit_port else ""), parts.path or "/",
                         parts.query, ""))
    host_header = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if explicit_port else "")
    extensions = {"sni_hostname": host} if scheme == "https" and not _is_ip(host) else {}
    return pinned, {"Host": host_header}, extensions


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


# --- Alta

def validate_events(events: list[str]) -> list[str]:
    unknown = sorted(set(events) - set(WEBHOOK_EVENTS))
    if unknown or not events:
        raise ValueError(f"Eventos no válidos: {', '.join(unknown) or '(ninguno)'}; "
                         f"se pueden usar {', '.join(WEBHOOK_EVENTS)}")
    return sorted(set(events))


def seal_secret(secret: str, *, webhook_id: int, key: bytes) -> str:
    return encrypt(secret, key=key, context=secret_context(webhook_id))


# --- Cola

async def enqueue(deps: Deps, env: dict) -> int:
    """Una entrega pendiente por cada webhook activo suscrito al evento."""
    organization_id = env.get("organization_id")
    scope = Webhook.organization_id.is_(None)
    if organization_id is not None:
        scope = or_(scope, Webhook.organization_id == organization_id)
    async with deps.sessions() as db:
        hooks = (await db.execute(select(Webhook.id, Webhook.events).where(
            Webhook.is_active.is_(True), Webhook.deleted_at.is_(None), scope))).all()
        targets = [hook_id for hook_id, events in hooks if env["type"] in (events or [])]
        if not targets:
            return 0
        payload = dumps(env)
        # DATETIME sin fracciones redondea: sin quitarlas, podría quedar un segundo en el futuro
        due = utcnow().replace(microsecond=0)
        db.add_all([WebhookDelivery(webhook_id=hook_id, event_id=env["id"], event=env["type"], payload=payload,
                                    next_attempt_at=due) for hook_id in targets])
        await db.commit()
    return len(targets)


@dataclass
class Result:
    ok: bool
    status_code: int | None
    error: str | None
    duration_ms: int


async def send(client: httpx.AsyncClient, url: str, secret: str, delivery_id: int, event: str, payload: str, *,
               private_hosts: frozenset[str], timeout: float) -> Result:
    """Un intento de entrega. Nunca lanza: el resultado dice qué pasó (sin detalles del receptor)."""
    started = time.perf_counter()

    def done(ok: bool, status: int | None, error: str | None) -> Result:
        return Result(ok, status, error, int((time.perf_counter() - started) * 1000))

    try:
        pinned, host_header, extensions = await pinned_request(url, private_hosts)
    except WebhookUrlError as exc:
        log.warning("webhook.url_blocked", delivery_id=delivery_id, reason=str(exc))
        return done(False, None, "url_bloqueada")
    headers = {**host_header, "Content-Type": "application/json", "User-Agent": USER_AGENT,
               "X-Platia-Event": event, "X-Platia-Delivery": str(delivery_id),
               "X-Platia-Signature": sign(secret, int(time.time()), payload)}
    try:
        response = await client.post(pinned, content=payload.encode(), headers=headers, extensions=extensions,
                                     timeout=timeout, follow_redirects=False)
    except httpx.TimeoutException:
        return done(False, None, "timeout")
    except httpx.HTTPError as exc:
        return done(False, None, f"conexion:{type(exc).__name__}"[:64])
    if 200 <= response.status_code < 300:
        return done(True, response.status_code, None)
    return done(False, response.status_code, f"http_{response.status_code}")


async def _claim(deps: Deps) -> list[tuple[int, int, str, str, int]]:
    now = utcnow()
    lease = now + timedelta(seconds=deps.settings.webhook_timeout_seconds + _LEASE_MARGIN)
    async with deps.sessions() as db, db.begin():
        rows = (await db.execute(select(WebhookDelivery).where(
            WebhookDelivery.status == DeliveryStatus.PENDING, WebhookDelivery.next_attempt_at <= now
        ).order_by(WebhookDelivery.id).limit(BATCH).with_for_update(skip_locked=True))).scalars().all()
        for row in rows:
            row.attempts += 1
            row.next_attempt_at = lease  # si el proceso muere a medio enviar, se reintenta tras el margen
        return [(r.id, r.webhook_id, r.event, r.payload, r.attempts) for r in rows]


async def deliver_one(deps: Deps, client: httpx.AsyncClient, claimed: tuple[int, int, str, str, int]) -> Result | None:
    delivery_id, webhook_id, event, payload, attempts = claimed
    settings = deps.settings
    async with deps.sessions() as db:
        hook = await db.get(Webhook, webhook_id)
    if hook is None or not hook.is_active or hook.deleted_at is not None or deps.encryption_key is None:
        async with deps.sessions() as db, db.begin():
            await db.execute(update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(
                status=DeliveryStatus.CANCELLED,
                last_error="sin_clave_de_cifrado" if deps.encryption_key is None else "webhook_inactivo"))
        return None
    secret = decrypt(hook.secret_encrypted, key=deps.encryption_key, context=secret_context(hook.id))
    result = await send(client, hook.url, secret, delivery_id, event, payload,
                        private_hosts=settings.webhook_private_host_set, timeout=settings.webhook_timeout_seconds)
    WEBHOOK_DELIVERIES.labels("ok" if result.ok else "error").inc()
    await record(deps, delivery_id, hook.id, attempts, result)
    return result


async def record(deps: Deps, delivery_id: int, webhook_id: int, attempts: int, result: Result, *,
                 retry: bool = True) -> None:
    """Guarda el resultado del intento. Sin `retry` (prueba manual), un fallo es definitivo y
    no cuenta para desactivar el webhook."""
    settings = deps.settings
    now = utcnow()
    values = {"last_status_code": result.status_code, "last_error": result.error,
              "last_duration_ms": result.duration_ms}
    async with deps.sessions() as db, db.begin():
        hook = await db.get(Webhook, webhook_id, with_for_update=True)
        if result.ok:
            values.update(status=DeliveryStatus.DELIVERED, delivered_at=now)
            hook.consecutive_failures = 0
            log.info("webhook.delivered", delivery_id=delivery_id, webhook_id=webhook_id, attempts=attempts,
                     status=result.status_code, duration_ms=result.duration_ms)
        elif not retry:
            values["status"] = DeliveryStatus.FAILED
        elif attempts >= settings.webhook_max_attempts:
            values["status"] = DeliveryStatus.FAILED
            hook.consecutive_failures += 1
            log.warning("webhook.failed", delivery_id=delivery_id, webhook_id=webhook_id, attempts=attempts,
                        error=result.error, status=result.status_code)
            if hook.consecutive_failures >= DISABLE_AFTER and hook.is_active:
                hook.is_active, hook.disabled_reason = False, "entregas_fallidas"
                log.error("webhook.disabled", webhook_id=webhook_id, consecutive_failures=hook.consecutive_failures)
        else:
            wait = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
            values["next_attempt_at"] = now + timedelta(seconds=wait)
            log.info("webhook.retry", delivery_id=delivery_id, webhook_id=webhook_id, attempts=attempts,
                     error=result.error, status=result.status_code, retry_in=wait)
        await db.execute(update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(**values))


async def deliver_due(deps: Deps, client: httpx.AsyncClient) -> int:
    """Envía las entregas que tocan ahora. Devuelve cuántas ha reclamado."""
    claimed = await _claim(deps)
    if claimed:
        await asyncio.gather(*(deliver_one(deps, client, c) for c in claimed))
    return len(claimed)


async def purge(deps: Deps) -> int:
    """Borra las entregas terminadas más antiguas que `WEBHOOK_RETENTION_DAYS`."""
    cutoff = utcnow() - timedelta(days=deps.settings.webhook_retention_days)
    async with deps.sessions() as db, db.begin():
        result = await db.execute(delete(WebhookDelivery).where(
            WebhookDelivery.created_at < cutoff, WebhookDelivery.status != DeliveryStatus.PENDING))
    return result.rowcount or 0


def client(settings) -> httpx.AsyncClient:
    # Sin proxy del entorno: la dirección ya comprobada debe ser la que se conecta
    return httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False, trust_env=False)


async def deliver_forever(deps: Deps, http: httpx.AsyncClient) -> None:
    """Bucle de entrega del proceso. Un fallo va al log y se reintenta en la vuelta siguiente."""
    settings = deps.settings
    last_purge = 0.0
    while True:
        try:
            while await deliver_due(deps, http) >= BATCH:
                pass
            if time.monotonic() - last_purge > 3600:
                last_purge = time.monotonic()
                await purge(deps)
        except Exception as exc:  # noqa: BLE001
            log.error("webhook.loop_failed", error_type=type(exc).__name__)
        await asyncio.sleep(settings.webhook_poll_seconds)


async def send_test(deps: Deps, http: httpx.AsyncClient, webhook_id: int) -> Result:
    """Envía ahora un evento `webhook.test` (`cli webhook-test`), aunque el webhook esté
    desactivado, y lo deja en las entregas. No se reintenta."""
    from .events import WEBHOOK_TEST, envelope

    settings = deps.settings
    async with deps.sessions() as db, db.begin():
        hook = await db.get(Webhook, webhook_id)
        if hook is None or hook.deleted_at is not None:
            raise ValueError(f"No existe el webhook {webhook_id}")
        env = envelope(WEBHOOK_TEST, hook.organization_id, {"webhook_id": hook.id, "message": "Prueba de entrega"})
        # Reclamada desde el principio: el bucle de entregas no la toca
        delivery = WebhookDelivery(webhook_id=hook.id, event_id=env["id"], event=WEBHOOK_TEST, payload=dumps(env),
                                   attempts=1, next_attempt_at=utcnow() + timedelta(days=1))
        db.add(delivery)
        await db.flush()
        delivery_id, url, sealed = delivery.id, hook.url, hook.secret_encrypted
    secret = decrypt(sealed, key=deps.encryption_key, context=secret_context(webhook_id))
    result = await send(http, url, secret, delivery_id, WEBHOOK_TEST, dumps(env),
                        private_hosts=settings.webhook_private_host_set, timeout=settings.webhook_timeout_seconds)
    await record(deps, delivery_id, webhook_id, 1, result, retry=False)
    return result

