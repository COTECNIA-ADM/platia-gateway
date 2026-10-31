"""Contadores de cuota en Redis: periodos, claves, estimación de tokens y guiones atómicos (cap. 13 a 15, ADR-011).

Cada contador cuenta una métrica (gasto, peticiones o tokens) de un ámbito (organización,
equipo, aplicación o persona) en una ventana: el minuto, el día o el mes naturales en UTC.
La clave lleva el inicio de la ventana, así que al cambiar de periodo empieza un contador
nuevo sin borrar nada, y caduca sola poco después del final.

El gasto se guarda en millonésimas de dólar (enteros: Redis suma enteros sin redondeos).
Los contadores de día y de mes se reconstruyen desde la auditoría de MySQL si faltan (Redis
reiniciado, límite recién creado); los de minuto, no: empiezan en cero.

Las peticiones simultáneas son un contador sin periodo que sube al admitir y baja al
terminar, con una caducidad de seguridad por si un proceso muere sin bajarlo.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .models import LimitMetric, LimitPeriod

MICRO = Decimal(1_000_000)
# Margen de caducidad de un contador tras el final de su ventana
_GRACE_SECONDS = 3600
# Estimación de tokens antes de llamar: unos 4 caracteres por token en texto mixto
_CHARS_PER_TOKEN = 4
# Salida prevista si la petición no fija un máximo ni el modelo lo tiene
_DEFAULT_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class Window:
    stamp: str  # va en la clave: identifica la ventana
    start: datetime | None  # None en las peticiones simultáneas
    end: datetime | None


def window(period: LimitPeriod, now: datetime) -> Window:
    """Ventana natural en UTC que contiene `now` (fecha sin zona, como `utcnow()`)."""
    if period == LimitPeriod.MINUTE:
        start = now.replace(second=0, microsecond=0)
        return Window(start.strftime("%Y%m%d%H%M"), start, start + timedelta(minutes=1))
    if period == LimitPeriod.DAY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return Window(start.strftime("%Y%m%d"), start, start + timedelta(days=1))
    if period == LimitPeriod.MONTH:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return Window(start.strftime("%Y%m"), start, end)
    return Window("live", None, None)


def ttl_seconds(win: Window, now: datetime, concurrent_ttl: int) -> int:
    if win.end is None:
        return concurrent_ttl
    return max(1, int((win.end - now).total_seconds())) + _GRACE_SECONDS


def counter_key(metric: LimitMetric, scope: str, scope_id: int, subject: str | None, win: Window) -> str:
    who = f"{scope}:{scope_id}" + (f":{subject}" if subject else "")
    return f"gw:q:{metric}:{who}:{win.stamp}"


def to_units(metric: LimitMetric, value: Decimal) -> int:
    """El límite o el gasto en las unidades del contador (millonésimas de USD, o unidades)."""
    if metric == LimitMetric.COST_USD:
        return int((Decimal(value) * MICRO).to_integral_value(rounding=ROUND_HALF_UP))
    return int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))


def from_units(metric: LimitMetric, units: int) -> Decimal:
    return Decimal(units) / MICRO if metric == LimitMetric.COST_USD else Decimal(units)


def estimate_tokens(body: dict, max_output_tokens: int | None) -> int:
    """Tokens que puede consumir la petición: la entrada por su tamaño y la salida por su máximo.

    Es una cota grosera a propósito (sin tokenizador): sirve para no dejar pasar ráfagas de
    peticiones grandes dentro del mismo minuto. Al terminar se corrige con el uso real.
    """
    text = json.dumps(body.get("messages", ""), ensure_ascii=False) + json.dumps(body.get("system", ""),
                                                                                 ensure_ascii=False)
    requested = body.get("max_tokens") or body.get("max_completion_tokens")
    output = requested if isinstance(requested, int) and requested > 0 else (max_output_tokens or _DEFAULT_OUTPUT_TOKENS)
    return len(text) // _CHARS_PER_TOKEN + output


# Admisión: comprueba todos los contadores y, solo si ninguno está agotado, los sube todos.
# Todo o nada en una sola llamada: dos procesos no pueden colarse a la vez por el último hueco.
# Adaptado del INCR con caducidad de machinebooks.ai/ES/aigateway/cap-15/block-13.lua (MIT):
# aquí se comprueban varios contadores y la caducidad se renueva en cada subida.
# KEYS: contadores. ARGV: por cada uno, límite, incremento y caducidad en segundos.
# Devuelve el número (desde 1) del primer contador agotado o la lista de valores nuevos.
ADMIT_LUA = """
for i = 1, #KEYS do
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if current >= tonumber(ARGV[3 * i - 2]) then
    return i
  end
end
local values = {}
for i = 1, #KEYS do
  values[i] = redis.call('INCRBY', KEYS[i], tonumber(ARGV[3 * i - 1]))
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[3 * i]))
end
return values
"""

# Cierre: suma a cada contador lo que de verdad se gastó (o resta lo reservado de más). Un
# contador que ya no existe (caducado o Redis reiniciado) no se recrea: si hace falta, se
# reconstruirá desde la auditoría. Las peticiones simultáneas nunca bajan de cero.
# KEYS: contadores. ARGV: la diferencia de cada uno. Devuelve los valores nuevos (-1 si no existía).
SETTLE_LUA = """
local values = {}
for i = 1, #KEYS do
  if redis.call('EXISTS', KEYS[i]) == 1 then
    local value = redis.call('INCRBY', KEYS[i], tonumber(ARGV[i]))
    if value < 0 then
      redis.call('SET', KEYS[i], 0, 'KEEPTTL')
      value = 0
    end
    values[i] = value
  else
    values[i] = -1
  end
end
return values
"""
