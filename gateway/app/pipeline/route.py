"""Etapa `route`: qué despliegue atiende la petición y con qué cuerpo (cap. 5 y 7, ADR-002).

Candidatos: despliegues activos del modelo cuyo proveedor está activo y permitido para la
organización. Un candidato de la otra familia solo es elegible si la petición se puede
traducir sin perder nada. Entre los elegibles se toma la prioridad más baja y, dentro de
ella, uno al azar según su peso. La reserva entre destinos y la salud llegan en G6.
"""
import random

import structlog
from sqlalchemy import select

from ..contracts import Unsupported, from_anthropic, from_openai, native_contract, to_anthropic, to_openai
from ..crypto import CryptoError, decrypt
from ..errors import InvalidRequest, Unavailable, UpstreamFailed
from ..models import Contract, Deployment, Provider
from ..providers import Target
from .context import Deps, PipelineContext

log = structlog.get_logger(__name__)


def prepare_body(ctx: PipelineContext, deployment: Deployment, provider: Provider) -> dict:
    """Cuerpo para el proveedor: el original si es su contrato; si no, traducido (o `Unsupported`)."""
    if native_contract(provider.kind) == ctx.contract:
        body = {**ctx.body, "model": deployment.upstream_model}
        if ctx.stream and ctx.contract == Contract.OPENAI:
            # Sin esto, OpenAI no manda el uso en streaming y la auditoría no tendría tokens
            options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            ctx.hide_usage_chunk = not options.get("include_usage")
            body["stream_options"] = {**options, "include_usage": True}
        return body
    neutral = from_anthropic(ctx.body) if ctx.contract == Contract.ANTHROPIC else from_openai(ctx.body)
    if ctx.contract == Contract.ANTHROPIC:
        return to_openai(neutral, deployment.upstream_model)
    return to_anthropic(neutral, deployment.upstream_model, ctx.model.max_output_tokens)


def pick(eligible: list[tuple[Deployment, Provider, dict]]) -> tuple[Deployment, Provider, dict]:
    top = min(d.priority for d, _, _ in eligible)
    tier = [item for item in eligible if item[0].priority == top]
    weights = [max(d.weight, 0) for d, _, _ in tier]
    if not any(weights):
        return tier[0]
    return random.choices(tier, weights=weights, k=1)[0]


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


async def run(ctx: PipelineContext, deps: Deps) -> None:
    async with deps.sessions() as db:
        rows = (await db.execute(
            select(Deployment, Provider).join(Provider, Deployment.provider_id == Provider.id)
            .where(Deployment.model_id == ctx.model.id, Deployment.is_active.is_(True),
                   Deployment.deleted_at.is_(None), Provider.is_active.is_(True), Provider.deleted_at.is_(None))
            .order_by(Deployment.priority, Deployment.id)
        )).all()
    allowed = ctx.identity.allowed_providers
    candidates = [(d, p) for d, p in rows if allowed is None or p.slug in allowed]
    if not candidates:
        raise Unavailable(f"El modelo '{ctx.model.key}' no tiene ningún destino disponible")

    eligible, reasons = [], []
    for deployment, provider in candidates:
        try:
            eligible.append((deployment, provider, prepare_body(ctx, deployment, provider)))
        except Unsupported as exc:
            reasons.append(str(exc))
    if not eligible:
        raise InvalidRequest(
            f"El modelo '{ctx.model.key}' se sirve con el contrato de otra familia y esta petición no se puede "
            f"traducir sin perder información ({reasons[0]}). Usa el otro contrato o solo texto.")

    deployment, provider, body = pick(eligible)
    ctx.target = Target(deployment_id=deployment.id, provider_slug=provider.slug, kind=provider.kind,
                        base_url=provider.base_url, upstream_model=deployment.upstream_model,
                        api_key=provider_key(deps, provider))
    ctx.upstream_body = body
