"""Enfriamiento de despliegues que fallan (cap. 6, ADR-010).

Salud pasiva: se aprende de las llamadas reales, sin pings que cuesten dinero. Un
despliegue se enfría con `COOLDOWN_FAILURES` fallos seguidos en `COOLDOWN_WINDOW_SECONDS`,
o a la primera con un 429/529 del proveedor (lo que pida su `Retry-After`, hasta
`COOLDOWN_MAX_SECONDS`). Un despliegue enfriado no se descarta: pasa al final de la cola,
para que un modelo con un solo destino no se quede sin servicio por un 429 aislado.

El estado vive en Redis con caducidad, compartido por todos los procesos del gateway. Si
Redis no responde, se sigue sin enfriamiento (es una optimización, no una política): se
avisa en el log y la petición sale igual.
"""
import structlog
from redis.exceptions import RedisError

from .errors import GatewayError
from .settings import Settings

log = structlog.get_logger(__name__)

_COOLED = "gw:cooldown:{}"
_FAILURES = "gw:failures:{}"


async def cooled(redis, deployment_ids: list[int]) -> set[int]:
    """Los despliegues de la lista que están enfriados ahora."""
    if redis is None or not deployment_ids:
        return set()
    try:
        values = await redis.mget([_COOLED.format(i) for i in deployment_ids])
    except (RedisError, OSError) as exc:
        log.warning("cooldown.unavailable", error_type=type(exc).__name__)
        return set()
    return {i for i, value in zip(deployment_ids, values, strict=True) if value is not None}


async def _cool(redis, deployment_id: int, seconds: int, reason: str) -> None:
    await redis.set(_COOLED.format(deployment_id), reason, ex=max(1, seconds))
    await redis.delete(_FAILURES.format(deployment_id))
    log.warning("deployment.cooled", deployment_id=deployment_id, seconds=seconds, reason=reason)


async def record_failure(redis, settings: Settings, deployment_id: int, error: GatewayError) -> tuple[int, str] | None:
    """Apunta el fallo. Si el despliegue se enfría ahora, devuelve los segundos y el motivo (para avisar)."""
    if redis is None:
        return None
    try:
        if error.rate_limited:
            seconds = int(min(error.retry_after or settings.cooldown_seconds, settings.cooldown_max_seconds))
            await _cool(redis, deployment_id, seconds, "rate_limit")
            return seconds, "rate_limit"
        key = _FAILURES.format(deployment_id)
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, settings.cooldown_window_seconds)
        if count >= settings.cooldown_failures:
            await _cool(redis, deployment_id, settings.cooldown_seconds, error.kind)
            return settings.cooldown_seconds, error.kind
    except (RedisError, OSError) as exc:
        log.warning("cooldown.unavailable", error_type=type(exc).__name__)
    return None


async def record_success(redis, deployment_id: int) -> None:
    """Un éxito reinicia la cuenta: lo que enfría son los fallos seguidos."""
    if redis is None:
        return
    try:
        await redis.delete(_FAILURES.format(deployment_id))
    except (RedisError, OSError) as exc:
        log.warning("cooldown.unavailable", error_type=type(exc).__name__)
