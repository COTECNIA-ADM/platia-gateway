"""Auditoría verificable: huellas con sal, cadena SHA-256, sellado, conciliación y verificación (cap. 21, ADR-013).

- **Huellas**: el prompt (`prompt_hash`) y la persona (`end_user_digest`) se guardan como
  HMAC-SHA256 con la sal de su organización. Sirven para comprobar que un texto o una
  persona concretos aparecen, sin guardar el texto (RNF-05) y sin que el mismo texto dé la
  misma huella en dos organizaciones.
- **Cadena**: cada fila cerrada se sella con `sha256(hash anterior | contenido canónico)`,
  por orden de id. Cambiar, borrar o intercalar una fila rompe la cadena desde ese punto.
  El contenido canónico deja fuera `end_user_ref`, para poder seudonimizarlo (RGPD) sin
  romperla: la cadena cubre su huella.
- **Sellado**: lo hace un proceso en segundo plano del gateway (y `cli audit-seal`). Una
  fila pendiente detiene el sellado hasta que se cierra o se concilia, para no dejar huecos.
- **Conciliación**: una fila que sigue pendiente pasado `AUDIT_PENDING_MAX_SECONDS` (el
  cierre falló o el proceso murió) se cierra como `error` / `audit_unclosed`.

La idea de la cadena con hash anterior y de la verificación por tramos sigue
`machinebooks.ai/ES/aigateway/cap-21/block-03.py` y `block-07.py` (MIT); aquí se sella
después de cerrar la fila, porque la auditoría se escribe en dos pasos (ADR-007).
"""
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditChainState, AuditLog, AuditStatus
from .models.base import utcnow

log = structlog.get_logger(__name__)

GENESIS = "0" * 64
HASH_VERSION = "v1"
SEAL_BATCH = 500

# Lo que cubre la cadena, en este orden. Añadir una columna cambia los hashes: va con una
# versión nueva (hash_version) y no se aplica a las filas ya selladas.
CHAIN_FIELDS = (
    "id", "request_id", "created_at", "completed_at", "organization_id", "team_id", "application_id", "user_id",
    "end_user_digest", "api_key_prefix", "purpose", "contract", "stream", "requested_model", "model_key",
    "deployment_id", "provider_slug", "status", "http_status", "error_type", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "cost_usd", "latency_ms", "attempts", "prompt_hash",
    "hash_version", "security_flags",
)


def keyed_digest(salt: str, value: str) -> str:
    return hmac.new(salt.encode(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def prompt_digest(salt: str, body: dict) -> str:
    """Huella de la petición tal como sale hacia el proveedor (ya filtrada), en JSON canónico."""
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return keyed_digest(salt, canonical)


def _canonical_value(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, Decimal):
        return f"{value:.8f}"
    if hasattr(value, "value"):  # enumeraciones
        return value.value
    return value


def row_payload(row: AuditLog) -> str:
    data = {name: _canonical_value(getattr(row, name)) for name in CHAIN_FIELDS}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(previous: str, row: AuditLog) -> str:
    return hashlib.sha256(f"{previous}|{row_payload(row)}".encode()).hexdigest()


async def _state(db: AsyncSession) -> AuditChainState:
    state = (await db.execute(select(AuditChainState).where(AuditChainState.id == 1).with_for_update())).scalar()
    if state is None:
        state = AuditChainState(id=1, last_id=0, last_hash=GENESIS)
        db.add(state)
        await db.flush()
    return state


async def reconcile(db: AsyncSession, max_pending_seconds: float) -> int:
    """Cierra como error las filas pendientes demasiado tiempo. Devuelve cuántas."""
    limit = utcnow() - timedelta(seconds=max_pending_seconds)
    result = await db.execute(
        update(AuditLog).where(AuditLog.status == AuditStatus.PENDING, AuditLog.created_at < limit,
                               AuditLog.chain_hash.is_(None))
        .values(status=AuditStatus.ERROR, error_type="audit_unclosed", completed_at=utcnow()))
    if result.rowcount:
        log.warning("audit.reconciled", rows=result.rowcount)
    return result.rowcount


async def seal(sessions, max_pending_seconds: float, batch: int = SEAL_BATCH) -> int:
    """Concilia y sella por orden las filas cerradas. Devuelve cuántas ha sellado.

    El estado de la cadena se lee con bloqueo: dos procesos no sellan a la vez.
    """
    sealed = 0
    async with sessions() as db, db.begin():
        await reconcile(db, max_pending_seconds)
        state = await _state(db)
        rows = (await db.execute(select(AuditLog).where(AuditLog.id > state.last_id).order_by(AuditLog.id)
                                 .limit(batch).with_for_update())).scalars().all()
        previous, last_id = state.last_hash, state.last_id
        for row in rows:
            if row.status == AuditStatus.PENDING:
                break  # se espera a que se cierre: la cadena va en orden y sin huecos
            if row.chain_hash is None:
                row.chain_hash = chain_hash(previous, row)
            previous, last_id = row.chain_hash, row.id
            sealed += 1
        if sealed:
            state.last_id, state.last_hash = last_id, previous
            log.info("audit.sealed", rows=sealed, last_id=last_id, last_hash=previous)
    return sealed


@dataclass
class Verification:
    ok: bool
    checked: int
    last_id: int
    message: str
    broken_at: int | None = None


async def verify(sessions, batch: int = 2000) -> Verification:
    """Recalcula la cadena entera y la compara con lo guardado y con el último eslabón."""
    previous, last_id, checked = GENESIS, 0, 0
    async with sessions() as db:
        state = (await db.execute(select(AuditChainState).where(AuditChainState.id == 1))).scalar()
        while True:
            rows = (await db.execute(select(AuditLog).where(AuditLog.id > last_id, AuditLog.chain_hash.is_not(None))
                                     .order_by(AuditLog.id).limit(batch))).scalars().all()
            if not rows:
                break
            for row in rows:
                if row.chain_hash != chain_hash(previous, row):
                    return Verification(False, checked, last_id, "La fila no coincide con su hash: se ha cambiado "
                                        "o falta una fila anterior", broken_at=row.id)
                previous, last_id = row.chain_hash, row.id
                checked += 1
            db.expunge_all()
        unsealed_before = (await db.execute(select(AuditLog.id).where(
            AuditLog.id < last_id, AuditLog.chain_hash.is_(None)).limit(1))).scalar()
    if unsealed_before is not None:
        return Verification(False, checked, last_id, "Hay una fila sin sellar entre filas selladas",
                            broken_at=unsealed_before)
    expected = (state.last_id, state.last_hash) if state else (0, GENESIS)
    if (last_id, previous) != expected:
        return Verification(False, checked, last_id, "La cadena no termina en el último eslabón registrado: "
                            "faltan filas del final", broken_at=expected[0])
    return Verification(True, checked, last_id, "Cadena íntegra")
