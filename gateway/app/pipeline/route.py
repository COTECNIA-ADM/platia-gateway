"""Etapa `route`: qué destinos pueden atender la petición y en qué orden (cap. 5, 6 y 7; ADR-002 y ADR-010).

Destinos de un modelo: sus despliegues activos cuyo proveedor está activo y permitido para la
organización. Uno de la otra familia solo vale si la petición se puede traducir sin perder
nada. Se ordenan por prioridad (0 primero) y, dentro de cada prioridad, al azar según su peso.

Detrás van los del primer modelo de la cadena de reserva, después los del segundo, etc.
Un modelo de reserva solo entra si la aplicación lo tiene permitido: la reserva nunca da a
una aplicación un modelo que no podía pedir. Los despliegues enfriados (`app.cooldown`)
pasan al final y la lista se corta en `ROUTE_MAX_ATTEMPTS`.

Esta etapa no llama al proveedor: `execute` prueba los destinos en orden.
"""
import random

import structlog
from sqlalchemy import select

from .. import cooldown
from ..contracts import Unsupported, from_anthropic, from_openai, native_contract, to_anthropic, to_openai
from ..crypto import CryptoError, decrypt
from ..errors import GatewayError, InvalidRequest, Unavailable, UpstreamFailed
from ..models import Contract, Deployment, LlmModel, Provider
from ..providers import Target
from .context import Candidate, Deps, ModelInfo, PipelineContext

log = structlog.get_logger(__name__)


def prepare_body(ctx: PipelineContext, deployment: Deployment, provider: Provider,
                 model: ModelInfo) -> tuple[dict, bool]:
    """Cuerpo para el proveedor (el original si es su contrato; si no, traducido o `Unsupported`)
    y si hay que ocultar al cliente el fragmento de uso que pide el gateway."""
    if native_contract(provider.kind) == ctx.contract:
        body = {**ctx.body, "model": deployment.upstream_model}
        hide_usage = False
        if ctx.stream and ctx.contract == Contract.OPENAI:
            # Sin esto, OpenAI no manda el uso en streaming y la auditoría no tendría tokens
            options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            hide_usage = not options.get("include_usage")
            body["stream_options"] = {**options, "include_usage": True}
        return body, hide_usage
    neutral = from_anthropic(ctx.body) if ctx.contract == Contract.ANTHROPIC else from_openai(ctx.body)
    if ctx.contract == Contract.ANTHROPIC:
        return to_openai(neutral, deployment.upstream_model), False
    return to_anthropic(neutral, deployment.upstream_model, model.max_output_tokens), False


def order(items: list, deployment_of=lambda item: item) -> list:
    """Prioridad más baja primero; dentro de cada prioridad, sorteo sin reemplazo según el peso.

    Los de peso 0 van al final de su prioridad (solo como reserva), en el orden recibido.
    """
    by_priority: dict[int, list] = {}
    for item in items:
        by_priority.setdefault(deployment_of(item).priority, []).append(item)
    result = []
    for priority in sorted(by_priority):
        tier = by_priority[priority]
        weighted = [item for item in tier if deployment_of(item).weight > 0]
        while weighted:
            chosen = random.choices(weighted, weights=[deployment_of(i).weight for i in weighted], k=1)[0]
            weighted.remove(chosen)
            result.append(chosen)
        result.extend(item for item in tier if deployment_of(item).weight <= 0)
    return result


def provider_key(deps: Deps, provider: Provider) -> str | None:
    if not provider.api_key_encrypted:
        return None  # proveedor sin clave (p. ej. un servidor local compatible con OpenAI)
    if deps.encryption_key is None:
        log.error("provider.key_unreadable", provider=provider.slug, reason="sin ENCRYPTION_KEY")
        raise UpstreamFailed("El proveedor no está bien configurado en el gateway")
    try:
        return decrypt(provider.api_key_encrypted, key=deps.encryption_key, context=f"provider:{provider.slug}")
    except CryptoError as exc:
        log.error("provider.key_unreadable", provider=provider.slug, reason=str(exc))
        raise UpstreamFailed("El proveedor no está bien configurado en el gateway") from exc


async def _chain(ctx: PipelineContext, deps: Deps, db) -> list[ModelInfo]:
    """El modelo pedido y sus reservas activas que la aplicación tiene permitidas, en orden."""
    chain = [ctx.model]
    if not ctx.model.fallbacks:
        return chain
    rows = (await db.execute(select(LlmModel).where(
        LlmModel.model_key.in_(ctx.model.fallbacks), LlmModel.is_active.is_(True), LlmModel.deleted_at.is_(None)
    ))).scalars().all()
    by_key = {m.model_key: m for m in rows}
    allowed = ctx.identity.allowed_models
    for key in ctx.model.fallbacks:
        model = by_key.get(key)
        if model is None or (allowed is not None and key not in allowed):
            continue
        chain.append(ModelInfo(id=model.id, key=model.model_key, max_output_tokens=model.max_output_tokens))
    return chain


async def run(ctx: PipelineContext, deps: Deps) -> None:
    async with deps.sessions() as db:
        chain = await _chain(ctx, deps, db)
        rows = (await db.execute(
            select(Deployment, Provider).join(Provider, Deployment.provider_id == Provider.id)
            .where(Deployment.model_id.in_([m.id for m in chain]), Deployment.is_active.is_(True),
                   Deployment.deleted_at.is_(None), Provider.is_active.is_(True), Provider.deleted_at.is_(None))
            .order_by(Deployment.priority, Deployment.id)
        )).all()
    allowed = ctx.identity.allowed_providers

    candidates: list[Candidate] = []
    reasons: list[str] = []
    key_error: GatewayError | None = None
    keys: dict[int, str | None] = {}
    for model in chain:
        pool = [(d, p) for d, p in rows if d.model_id == model.id and (allowed is None or p.slug in allowed)]
        for deployment, provider in order(pool, deployment_of=lambda item: item[0]):
            try:
                body, hide_usage = prepare_body(ctx, deployment, provider, model)
            except Unsupported as exc:
                reasons.append(str(exc))
                continue
            try:
                if provider.id not in keys:
                    keys[provider.id] = provider_key(deps, provider)
            except GatewayError as exc:  # clave ilegible: ese proveedor no es elegible
                key_error = exc
                continue
            candidates.append(Candidate(
                model=model, body=body, hide_usage_chunk=hide_usage,
                target=Target(deployment_id=deployment.id, provider_slug=provider.slug, kind=provider.kind,
                              base_url=provider.base_url, upstream_model=deployment.upstream_model,
                              api_key=keys[provider.id])))

    if not candidates:
        if reasons:
            raise InvalidRequest(
                f"El modelo '{ctx.model.key}' se sirve con el contrato de otra familia y esta petición no se puede "
                f"traducir sin perder información ({reasons[0]}). Usa el otro contrato o solo texto.")
        if key_error is not None:
            raise key_error
        raise Unavailable(f"El modelo '{ctx.model.key}' no tiene ningún destino disponible")

    # Los enfriados, al final: se prueban solo si los demás fallan
    cold = await cooldown.cooled(deps.redis, [c.target.deployment_id for c in candidates])
    if cold:
        candidates = [c for c in candidates if c.target.deployment_id not in cold] + \
                     [c for c in candidates if c.target.deployment_id in cold]
    ctx.candidates = candidates[:max(1, deps.settings.route_max_attempts)]
    first = ctx.candidates[0]
    ctx.target, ctx.upstream_body, ctx.hide_usage_chunk = first.target, first.body, first.hide_usage_chunk
