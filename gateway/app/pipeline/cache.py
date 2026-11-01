"""Etapa `cache`: respuestas repetidas sin volver a llamar al proveedor (G11, cap. 8, ADR-015).

Caché exacta: la misma petición (cuerpo ya filtrado, modelo y propósito) de la misma
aplicación y la misma persona final recibe la misma respuesta mientras dure
`applications.cache_ttl_seconds`. Cada aplicación decide si la usa (0 = no, lo de por
defecto) y para qué propósitos.

- Solo sin streaming, y solo respuestas terminadas con normalidad (no cortadas por
  `max_tokens` ni a medio usar una herramienta).
- La clave incluye organización, aplicación y persona final: nunca se sirve una respuesta
  a otra persona (cap. 8). Sin `ENCRYPTION_KEY` no se guarda nada.
- En Redis la respuesta va cifrada (AES-256-GCM) con la clave como dato asociado.
- Va después de `policy` (lo que ya no se permite no sale de la caché) y antes de `quota`:
  un acierto no cuesta nada. El filtro de salida se vuelve a aplicar en cada acierto, con
  la política vigente.
- Si Redis falla, se sigue sin caché: no es un control, es un ahorro.

La clave por persona y el cifrado siguen machinebooks.ai/ES/aigateway/cap-08/block-01.py y
block-02.py (MIT).
"""
import hashlib
import json

import structlog

from ..crypto import CryptoError, decrypt, encrypt
from ..models import Contract
from ..providers import PASSTHROUGH_HEADERS
from .context import Deps, PipelineContext

log = structlog.get_logger(__name__)

HEADER = "x-platia-cache"
PREFIX = "gw:cache"
KEY_VERSION = "v1"
# Respuestas terminadas con normalidad, por contrato
_DONE = {Contract.ANTHROPIC: {"end_turn", "stop_sequence"}, Contract.OPENAI: {"stop"}}


def _wanted(ctx: PipelineContext) -> bool:
    identity = ctx.identity
    if ctx.stream or identity.cache_ttl_seconds <= 0:
        return False
    if (ctx.headers.get(HEADER) or "").lower() in ("no", "bypass", "off"):
        return False
    purposes = identity.cache_purposes
    return purposes is None or ctx.purpose in purposes


def cache_key(ctx: PipelineContext) -> str:
    identity = ctx.identity
    # Las cabeceras que se reenvían al proveedor cambian la respuesta: entran en la clave
    headers = {name: ctx.headers.get(name) for name in PASSTHROUGH_HEADERS}
    material = json.dumps({"v": KEY_VERSION, "user": ctx.end_user_ref, "purpose": ctx.purpose,
                           "contract": str(ctx.contract), "headers": headers, "body": ctx.body},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{PREFIX}:{identity.organization_id}:{identity.application_id}:{digest}"


def _finished(ctx: PipelineContext, body: dict) -> bool:
    if ctx.contract == Contract.ANTHROPIC:
        reason = body.get("stop_reason")
    else:
        choices = body.get("choices") or []
        reason = choices[0].get("finish_reason") if len(choices) == 1 else None
    return reason in _DONE[ctx.contract]


async def run(ctx: PipelineContext, deps: Deps) -> None:
    """Busca la respuesta. Con acierto deja `ctx.response_body` y `ctx.cache_hit`."""
    ctx.cache_state = "off"
    if not _wanted(ctx) or deps.redis is None or deps.encryption_key is None:
        return
    key = cache_key(ctx)
    ctx.cache_key, ctx.cache_state = key, "miss"
    try:
        raw = await deps.redis.get(key)
    except Exception as exc:  # noqa: BLE001 - sin Redis se sigue sin caché
        log.warning("cache.unavailable", request_id=ctx.request_id, error_type=type(exc).__name__)
        ctx.cache_key = None
        return
    if raw is None:
        return
    try:
        body = json.loads(decrypt(raw.decode() if isinstance(raw, bytes) else raw, key=deps.encryption_key,
                                  context=key))
    except (CryptoError, ValueError, UnicodeDecodeError):
        log.warning("cache.unreadable", request_id=ctx.request_id)
        return
    ctx.response_body, ctx.cache_hit, ctx.cache_state = body, True, "hit"
    log.info("cache.hit", request_id=ctx.request_id, application=ctx.identity.application_slug,
             purpose=ctx.purpose)


async def store(ctx: PipelineContext, deps: Deps) -> None:
    """Guarda la respuesta tal como llegó del proveedor (antes del filtro de salida)."""
    if not ctx.cache_key or ctx.cache_hit or ctx.response_body is None or not _finished(ctx, ctx.response_body):
        return
    payload = json.dumps(ctx.response_body, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > deps.settings.cache_max_bytes:
        return
    try:
        sealed = encrypt(payload, key=deps.encryption_key, context=ctx.cache_key)
        await deps.redis.set(ctx.cache_key, sealed, ex=ctx.identity.cache_ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - guardar en la caché nunca rompe la respuesta
        log.warning("cache.store_failed", request_id=ctx.request_id, error_type=type(exc).__name__)


def pattern(organization_id: int, application_id: int | None = None) -> str:
    """Patrón de las claves de una organización (o de una de sus aplicaciones), para vaciarlas."""
    return f"{PREFIX}:{organization_id}:{application_id if application_id is not None else '*'}:*"
