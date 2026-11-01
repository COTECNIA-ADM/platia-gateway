"""Etapa `quota`: cuotas de gasto y límites de ritmo antes de llamar al proveedor (cap. 13 a 15, ADR-011).

Reúne los límites activos que tocan a la petición: los de su organización, su equipo, su
aplicación y su persona final (si la aplicación la declara). Un límite de una persona
concreta sustituye al general de las personas de esa aplicación.

- Gasto, peticiones o tokens por **día o mes** agotados: 402 (no sirve reintentar hasta que
  se renueve; la cabecera `X-Platia-Quota-Reset` dice cuándo).
- Peticiones o tokens por **minuto**, o **peticiones simultáneas**, al tope: 429 con `Retry-After`.

Se admite mientras el contador no haya llegado al límite: la última petición puede pasarse
un poco (su coste no se sabe hasta el final). El exceso está acotado por las peticiones en
vuelo. Al admitir se suma 1 petición, los tokens estimados y 1 simultánea; al terminar
(`settle`), los tokens reales, el coste real y se libera la simultánea.

Sin límites que comprobar no se toca Redis. Con límites, si Redis no responde se rechaza la
petición (503): fallar cerrado (ADR-005).
"""
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from redis.exceptions import RedisError
from sqlalchemy import and_, func, or_, select

from .. import events
from ..errors import QuotaExceeded, RateLimited, Unavailable
from ..limits import (ADMIT_LUA, SETTLE_LUA, Window, counter_key, estimate_tokens, from_units, to_units,
                      ttl_seconds, window)
from ..models import AuditLog, AuditStatus, LimitMetric, LimitPeriod, LimitScope, UsageLimit
from ..models.base import utcnow
from ..telemetry import QUOTA_REJECTIONS
from .context import Deps, PipelineContext

log = structlog.get_logger(__name__)

_SCOPE_NAMES = {LimitScope.ORGANIZATION: "la organización", LimitScope.TEAM: "el equipo",
                LimitScope.APPLICATION: "la aplicación", LimitScope.END_USER: "la persona"}
_METRIC_NAMES = {LimitMetric.COST_USD: "gasto", LimitMetric.REQUESTS: "peticiones",
                 LimitMetric.TOKENS: "tokens", LimitMetric.CONCURRENT: "peticiones simultáneas"}
_PERIOD_NAMES = {LimitPeriod.MINUTE: "por minuto", LimitPeriod.DAY: "diario", LimitPeriod.MONTH: "mensual",
                 LimitPeriod.NONE: ""}


@dataclass
class Counter:
    key: str
    limit_id: int
    scope: LimitScope
    scope_id: int
    subject: str | None
    metric: LimitMetric
    period: LimitPeriod
    window: Window
    limit: int  # en unidades del contador
    alert_pct: int
    ttl: int
    value: int = 0  # tras la admisión

    @property
    def is_rate(self) -> bool:
        return self.period in (LimitPeriod.MINUTE, LimitPeriod.NONE)


@dataclass
class QuotaState:
    counters: list[Counter]
    estimated_tokens: int
    settled: bool = False
    headers: dict[str, str] = field(default_factory=dict)


def _applicable(rows: Iterable[UsageLimit], end_user_ref: str | None) -> list[UsageLimit]:
    """Quita los límites generales de personas que tienen uno propio de la misma métrica y periodo."""
    rows = list(rows)
    personal = {(r.metric, r.period) for r in rows if r.scope == LimitScope.END_USER and r.subject == end_user_ref}
    return [r for r in rows if not (r.scope == LimitScope.END_USER and r.subject is None
                                    and (r.metric, r.period) in personal)]


async def _load(ctx: PipelineContext, deps: Deps) -> list[UsageLimit]:
    identity = ctx.identity
    scopes = [and_(UsageLimit.scope == LimitScope.ORGANIZATION, UsageLimit.scope_id == identity.organization_id),
              and_(UsageLimit.scope == LimitScope.TEAM, UsageLimit.scope_id == identity.team_id),
              and_(UsageLimit.scope == LimitScope.APPLICATION, UsageLimit.scope_id == identity.application_id)]
    if ctx.end_user_ref:
        scopes.append(and_(UsageLimit.scope == LimitScope.END_USER, UsageLimit.scope_id == identity.application_id,
                           or_(UsageLimit.subject.is_(None), UsageLimit.subject == ctx.end_user_ref)))
    async with deps.sessions() as db:
        rows = (await db.execute(select(UsageLimit).where(
            UsageLimit.is_active.is_(True), UsageLimit.deleted_at.is_(None), or_(*scopes)
        ).order_by(UsageLimit.id))).scalars().all()
    return _applicable(rows, ctx.end_user_ref)


def _counters(ctx: PipelineContext, deps: Deps, rows: list[UsageLimit], now: datetime) -> list[Counter]:
    concurrent_ttl = int(max(deps.settings.stream_max_seconds, deps.settings.provider_timeout_seconds)) + 60
    by_key: dict[str, Counter] = {}
    for row in rows:
        period = LimitPeriod.NONE if row.metric == LimitMetric.CONCURRENT else row.period
        # El contador de personas es de cada persona, aunque el límite sea el general
        subject = ctx.end_user_ref if row.scope == LimitScope.END_USER else None
        win = window(period, now)
        key = counter_key(row.metric, row.scope, row.scope_id, subject, win)
        counter = Counter(key=key, limit_id=row.id, scope=row.scope, scope_id=row.scope_id, subject=subject,
                          metric=row.metric, period=period, window=win, limit=to_units(row.metric, row.limit_value),
                          alert_pct=row.alert_pct, ttl=ttl_seconds(win, now, concurrent_ttl))
        # Dos límites sobre el mismo contador: manda el más estricto
        if key not in by_key or counter.limit < by_key[key].limit:
            by_key[key] = counter
    return list(by_key.values())


def _admission_increment(counter: Counter, estimated_tokens: int) -> int:
    return {LimitMetric.REQUESTS: 1, LimitMetric.TOKENS: estimated_tokens,
            LimitMetric.CONCURRENT: 1}.get(counter.metric, 0)


async def _rebuild(deps: Deps, counter: Counter) -> int:
    """Lo consumido en la ventana según la auditoría (para un contador de día o mes que falta en Redis)."""
    where = [AuditLog.created_at >= counter.window.start, AuditLog.status != AuditStatus.BLOCKED]
    if counter.scope == LimitScope.ORGANIZATION:
        where.append(AuditLog.organization_id == counter.scope_id)
    elif counter.scope == LimitScope.TEAM:
        where.append(AuditLog.team_id == counter.scope_id)
    else:
        where.append(AuditLog.application_id == counter.scope_id)
        if counter.scope == LimitScope.END_USER:
            where.append(AuditLog.end_user_ref == counter.subject)
    measure = {
        LimitMetric.COST_USD: func.sum(AuditLog.cost_usd),
        LimitMetric.REQUESTS: func.count(),
        LimitMetric.TOKENS: func.sum(AuditLog.input_tokens + AuditLog.output_tokens + AuditLog.cache_read_tokens
                                     + AuditLog.cache_write_tokens),
    }[counter.metric]
    async with deps.sessions() as db:
        total = (await db.execute(select(measure).where(*where))).scalar()
    return to_units(counter.metric, total or 0)


async def _seed(deps: Deps, counters: list[Counter]) -> None:
    periodic = [c for c in counters if c.period in (LimitPeriod.DAY, LimitPeriod.MONTH)]
    if not periodic:
        return
    present = await deps.redis.mget([c.key for c in periodic])
    for counter, value in zip(periodic, present, strict=True):
        if value is None:
            rebuilt = await _rebuild(deps, counter)
            # NX: si otro proceso lo acaba de crear, vale el suyo
            await deps.redis.set(counter.key, rebuilt, ex=counter.ttl, nx=True)
            log.info("quota.counter_rebuilt", limit_id=counter.limit_id, metric=counter.metric,
                     period=counter.period, value=rebuilt)


def _reject(ctx: PipelineContext, counter: Counter, now: datetime):
    what = f"{_METRIC_NAMES[counter.metric]} {_PERIOD_NAMES[counter.period]}".strip()
    limit = from_units(counter.metric, counter.limit)
    shown = f"{limit:.2f} USD" if counter.metric == LimitMetric.COST_USD else f"{int(limit)}"
    log.warning("quota.rejected", request_id=ctx.request_id, limit_id=counter.limit_id, scope=counter.scope,
                scope_id=counter.scope_id, metric=counter.metric, period=counter.period,
                application=ctx.identity.application_slug)
    QUOTA_REJECTIONS.labels(str(counter.scope), str(counter.metric), str(counter.period)).inc()
    message = f"Límite de {what} de {_SCOPE_NAMES[counter.scope]} alcanzado ({shown})"
    if counter.is_rate:
        retry = 1 if counter.window.end is None else max(1, int((counter.window.end - now).total_seconds()) + 1)
        return RateLimited(f"{message}; vuelve a intentarlo en {retry} s", retry_after=retry)
    reset = counter.window.end.strftime("%Y-%m-%dT%H:%M:%SZ")
    return QuotaExceeded(f"{message}; se renueva el {counter.window.end:%Y-%m-%d %H:%M} UTC",
                         headers={"X-Platia-Quota-Reset": reset})


def _headers(counters: list[Counter]) -> dict[str, str]:
    """Lo que queda del límite más estricto de peticiones por minuto y de gasto."""
    headers: dict[str, str] = {}
    requests = [c for c in counters if c.metric == LimitMetric.REQUESTS and c.period == LimitPeriod.MINUTE]
    if requests:
        tightest = min(requests, key=lambda c: c.limit - c.value)
        headers["X-Platia-RateLimit-Limit"] = str(tightest.limit)
        headers["X-Platia-RateLimit-Remaining"] = str(max(0, tightest.limit - tightest.value))
    budgets = [c for c in counters if c.metric == LimitMetric.COST_USD]
    if budgets:
        remaining = min(c.limit - c.value for c in budgets)
        headers["X-Platia-Budget-Remaining-Usd"] = f"{from_units(LimitMetric.COST_USD, max(0, remaining)):.6f}"
    return headers


async def run(ctx: PipelineContext, deps: Deps) -> None:
    rows = await _load(ctx, deps)
    if not rows:
        return
    if deps.redis is None:
        raise Unavailable("El gateway no puede comprobar las cuotas en este momento")
    now = utcnow()
    counters = _counters(ctx, deps, rows, now)
    estimated = estimate_tokens(ctx.body, ctx.model.max_output_tokens if ctx.model else None)
    args: list[int] = []
    for counter in counters:
        args += [counter.limit, _admission_increment(counter, estimated), counter.ttl]
    try:
        await _seed(deps, counters)
        result = await deps.redis.eval(ADMIT_LUA, len(counters), *[c.key for c in counters], *args)
    except (RedisError, OSError) as exc:
        log.error("quota.unavailable", request_id=ctx.request_id, error_type=type(exc).__name__)
        raise Unavailable("El gateway no puede comprobar las cuotas en este momento") from exc
    if isinstance(result, int):
        raise _reject(ctx, counters[result - 1], now)
    for counter, value in zip(counters, result, strict=True):
        counter.value = int(value)
    ctx.quota = QuotaState(counters=counters, estimated_tokens=estimated, headers=_headers(counters))
    # Las peticiones solo suben al admitir: su aviso se da aquí (el gasto y los tokens, en settle)
    try:
        for counter in counters:
            if counter.metric == LimitMetric.REQUESTS:
                await _alert(deps, ctx, counter, counter.value)
    except (RedisError, OSError) as exc:
        log.warning("quota.alert_failed", request_id=ctx.request_id, error_type=type(exc).__name__)


def _delta(counter: Counter, ctx: PipelineContext, real_tokens: int) -> int:
    if counter.metric == LimitMetric.TOKENS:
        return real_tokens - ctx.quota.estimated_tokens
    if counter.metric == LimitMetric.COST_USD:
        return to_units(LimitMetric.COST_USD, ctx.cost_usd)
    if counter.metric == LimitMetric.CONCURRENT:
        return -1
    return 0


async def _alert(deps: Deps, ctx: PipelineContext, counter: Counter, value: int) -> None:
    """Avisa, una vez por ventana, al pasar el umbral de aviso y al agotar el límite: en el log,
    en tiempo real y a los webhooks suscritos (G12)."""
    if counter.is_rate or counter.limit <= 0:
        return
    for threshold, event in ((100, events.QUOTA_EXHAUSTED), (counter.alert_pct, events.QUOTA_ALERT)):
        if value * 100 >= counter.limit * threshold:
            if await deps.redis.set(f"{counter.key}:alert:{threshold}", 1, ex=counter.ttl, nx=True):
                data = {"limit_id": counter.limit_id, "scope": str(counter.scope), "scope_id": counter.scope_id,
                        "subject": counter.subject, "metric": str(counter.metric), "period": str(counter.period),
                        "used": str(from_units(counter.metric, value)),
                        "limit": str(from_units(counter.metric, counter.limit)), "threshold_pct": threshold,
                        "window_end": counter.window.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "application": ctx.identity.application_slug}
                log.warning(event, **data)
                await events.emit(deps, event, organization_id=ctx.identity.organization_id, data=data)
            return


async def settle(ctx: PipelineContext, deps: Deps) -> None:
    """Ajusta los contadores con lo que de verdad se consumió y libera la petición simultánea.

    Se llama al cerrar la auditoría (también en streaming y en errores) y solo cuenta una vez.
    Un fallo aquí no cambia la respuesta: va al log y los contadores de día y de mes se
    corrigen solos al reconstruirse desde la auditoría.
    """
    state = ctx.quota
    if state is None or state.settled:
        return
    state.settled = True
    usage = ctx.usage
    real_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    pending = [(c, _delta(c, ctx, real_tokens)) for c in state.counters]
    pending = [(c, d) for c, d in pending if d]
    if not pending:
        return
    try:
        values = await deps.redis.eval(SETTLE_LUA, len(pending), *[c.key for c, _ in pending],
                                       *[d for _, d in pending])
        for (counter, _), value in zip(pending, values, strict=True):
            if int(value) >= 0:
                await _alert(deps, ctx, counter, int(value))
    except (RedisError, OSError) as exc:
        log.error("quota.settle_failed", request_id=ctx.request_id, error_type=type(exc).__name__)
