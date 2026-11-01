"""Catálogo común desde el panel: proveedores, modelos, despliegues y precios (G13, RF-16).

El catálogo es de todas las organizaciones: los administradores lo ven y solo quien opera el
gateway lo cambia. La clave de un proveedor se puede poner o cambiar, pero nunca se lee: se
guarda cifrada (`app/crypto.py`) y la respuesta solo dice si hay una. El estado de cada
despliegue (enfriado o no, G6) sale de Redis.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select

from .. import cooldown
from ..crypto import encrypt
from ..errors import InvalidRequest, NotFound, Unavailable
from ..identity import Principal
from ..models import Deployment, LlmModel, ModelFamily, ModelPrice, Provider, ProviderKind
from ..models.base import utcnow
from ..pipeline.runner import deps_of
from .common import Body, admin, commit, flush, get_live, iso, operator, plain, record

router = APIRouter(tags=["panel: catálogo"])
SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
MODEL_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class ProviderIn(Body):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=255)
    kind: ProviderKind
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    # http solo para un servicio de la red interna (p. ej. un modelo local)
    allow_http: bool = False


class ProviderPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    is_active: bool | None = None
    allow_http: bool = False


class ModelIn(Body):
    model_key: str = Field(pattern=MODEL_KEY)
    display_name: str = Field(min_length=1, max_length=255)
    family: ModelFamily
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    fallback_models: list[str] = Field(default_factory=list, max_length=10)


class ModelPatch(Body):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    capabilities: list[str] | None = Field(default=None, max_length=20)
    fallback_models: list[str] | None = Field(default=None, max_length=10)
    is_active: bool | None = None


class DeploymentIn(Body):
    model_id: int
    provider_id: int
    upstream_model: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int = Field(default=0, ge=0, le=1000)
    weight: int = Field(default=100, ge=0, le=10000)


class DeploymentPatch(Body):
    upstream_model: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = Field(default=None, ge=0, le=1000)
    weight: int | None = Field(default=None, ge=0, le=10000)
    is_active: bool | None = None


class PriceIn(Body):
    effective_from: datetime | None = None
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_per_mtok: float | None = Field(default=None, ge=0)


def _no_nulls(changes: dict, nullable: set[str] = frozenset()) -> None:
    for field, value in changes.items():
        if value is None and field not in nullable:
            raise InvalidRequest(f"'{field}' no puede ser nulo")


def _base_url(url: str | None, kind: ProviderKind, allow_http: bool) -> str | None:
    if kind == ProviderKind.OPENAI_COMPATIBLE and not url:
        raise InvalidRequest("Un proveedor compatible con OpenAI necesita base_url (p. ej. https://api.openai.com/v1)")
    if url and not url.startswith("https://") and not (allow_http and url.startswith("http://")):
        raise InvalidRequest("base_url debe ser https:// (allow_http solo para un servicio de la red interna)")
    return url


def _seal(request: Request, secret: str, slug: str) -> str:
    key = deps_of(request).encryption_key
    if key is None:
        raise Unavailable("Falta ENCRYPTION_KEY: no se puede guardar la clave del proveedor")
    return encrypt(secret, key=key, context=f"provider:{slug}")


def _provider_body(p: Provider) -> dict:
    return {"id": p.id, "slug": p.slug, "name": p.name, "kind": str(p.kind), "base_url": p.base_url,
            "has_api_key": bool(p.api_key_encrypted), "is_active": p.is_active, "updated_at": iso(p.updated_at)}


def _price_body(price: ModelPrice | None) -> dict | None:
    if price is None:
        return None
    return {"id": price.id, "effective_from": iso(price.effective_from), "input_per_mtok": plain(price.input_per_mtok),
            "output_per_mtok": plain(price.output_per_mtok),
            "cache_read_per_mtok": plain(price.cache_read_per_mtok),
            "cache_write_per_mtok": plain(price.cache_write_per_mtok)}


def _model_body(m: LlmModel, price: ModelPrice | None = None) -> dict:
    return {"id": m.id, "model_key": m.model_key, "display_name": m.display_name, "family": str(m.family),
            "context_window": m.context_window, "max_output_tokens": m.max_output_tokens,
            "capabilities": m.capabilities, "fallback_models": m.fallback_models or [], "is_active": m.is_active,
            "price": _price_body(price)}


def _deployment_body(d: Deployment, cooled: set[int]) -> dict:
    return {"id": d.id, "model_id": d.model_id, "provider_id": d.provider_id, "upstream_model": d.upstream_model,
            "priority": d.priority, "weight": d.weight, "is_active": d.is_active, "cooled": d.id in cooled}


async def _fallbacks(db, model_key: str, keys: list[str] | None) -> list[str] | None:
    chain: list[str] = []
    for key in keys or []:
        if key == model_key:
            raise InvalidRequest("Un modelo no puede ser su propia reserva")
        if key not in chain:
            chain.append(key)
    if chain:
        known = set((await db.execute(select(LlmModel.model_key).where(
            LlmModel.model_key.in_(chain), LlmModel.deleted_at.is_(None)))).scalars())
        missing = [k for k in chain if k not in known]
        if missing:
            raise InvalidRequest(f"Modelos de reserva que no están en el catálogo: {', '.join(missing)}")
    return chain or None


async def _current_prices(db) -> dict[int, ModelPrice]:
    now = utcnow()
    prices: dict[int, ModelPrice] = {}
    for price in (await db.execute(select(ModelPrice).where(ModelPrice.effective_from <= now)
                                   .order_by(ModelPrice.effective_from))).scalars():
        prices[price.model_id] = price  # la última que ya ha empezado
    return prices


@router.get("/catalog")
async def catalog(request: Request, who: Principal = Depends(admin)):
    deps = deps_of(request)
    async with deps.sessions() as db:
        providers = (await db.execute(select(Provider).where(Provider.deleted_at.is_(None))
                                      .order_by(Provider.slug))).scalars().all()
        models = (await db.execute(select(LlmModel).where(LlmModel.deleted_at.is_(None))
                                   .order_by(LlmModel.model_key))).scalars().all()
        deployments = (await db.execute(select(Deployment).where(Deployment.deleted_at.is_(None))
                                        .order_by(Deployment.model_id, Deployment.priority))).scalars().all()
        prices = await _current_prices(db)
    cooled = await cooldown.cooled(deps.redis, [d.id for d in deployments])
    return {"providers": [_provider_body(p) for p in providers],
            "models": [_model_body(m, prices.get(m.id)) for m in models],
            "deployments": [_deployment_body(d, cooled) for d in deployments],
            "can_edit": who.platform_operator}


# --- Proveedores

@router.post("/providers", status_code=201)
async def create_provider(request: Request, body: ProviderIn, who: Principal = Depends(operator)):
    url = _base_url(body.base_url, body.kind, body.allow_http)
    async with deps_of(request).sessions() as db:
        provider = Provider(slug=body.slug, name=body.name, kind=body.kind, base_url=url,
                            api_key_encrypted=_seal(request, body.api_key, body.slug) if body.api_key else None)
        db.add(provider)
        await flush(db, f"Ya existe un proveedor '{body.slug}'")
        record(db, request, who, "provider.create", "provider", provider.id,
               body.model_dump(exclude={"api_key"}) | ({"api_key": True} if body.api_key else {}),
               organization_id=None)
        await commit(db, f"Ya existe un proveedor '{body.slug}'")
        return _provider_body(provider)


@router.patch("/providers/{provider_id}")
async def update_provider(request: Request, provider_id: int, body: ProviderPatch, who: Principal = Depends(operator)):
    changes = body.model_dump(exclude_unset=True, exclude={"allow_http"})
    _no_nulls(changes, {"base_url"})
    async with deps_of(request).sessions() as db:
        provider = await get_live(db, Provider, provider_id, "el proveedor")
        if "base_url" in changes:
            provider.base_url = _base_url(changes["base_url"], provider.kind, body.allow_http)
        if "name" in changes:
            provider.name = changes["name"]
        if "is_active" in changes:
            provider.is_active = changes["is_active"]
        if "api_key" in changes:
            provider.api_key_encrypted = _seal(request, changes["api_key"], provider.slug)
        record(db, request, who, "provider.update", "provider", provider.id, changes, organization_id=None)
        await commit(db)
        return _provider_body(provider)


# --- Modelos y precios

@router.post("/models", status_code=201)
async def create_model(request: Request, body: ModelIn, who: Principal = Depends(operator)):
    async with deps_of(request).sessions() as db:
        model = LlmModel(model_key=body.model_key, display_name=body.display_name, family=body.family,
                         context_window=body.context_window, max_output_tokens=body.max_output_tokens,
                         capabilities=body.capabilities,
                         fallback_models=await _fallbacks(db, body.model_key, body.fallback_models))
        db.add(model)
        await flush(db, f"Ya existe el modelo '{body.model_key}'")
        record(db, request, who, "model.create", "model", model.id, body.model_dump(), organization_id=None)
        await commit(db, f"Ya existe el modelo '{body.model_key}'")
        return _model_body(model)


@router.patch("/models/{model_id}")
async def update_model(request: Request, model_id: int, body: ModelPatch, who: Principal = Depends(operator)):
    changes = body.model_dump(exclude_unset=True)
    _no_nulls(changes, {"context_window", "max_output_tokens"})
    async with deps_of(request).sessions() as db:
        model = await get_live(db, LlmModel, model_id, "el modelo")
        if "fallback_models" in changes:
            changes["fallback_models"] = await _fallbacks(db, model.model_key, changes["fallback_models"])
        for field, value in changes.items():
            setattr(model, field, value)
        record(db, request, who, "model.update", "model", model.id, changes, organization_id=None)
        await commit(db)
        prices = await _current_prices(db)
        return _model_body(model, prices.get(model.id))


@router.get("/models/{model_id}/prices")
async def list_prices(request: Request, model_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        model = await get_live(db, LlmModel, model_id, "el modelo")
        rows = (await db.execute(select(ModelPrice).where(ModelPrice.model_id == model.id)
                                 .order_by(ModelPrice.effective_from.desc()))).scalars()
        return {"data": [_price_body(p) for p in rows]}


@router.post("/models/{model_id}/prices", status_code=201)
async def add_price(request: Request, model_id: int, body: PriceIn, who: Principal = Depends(operator)):
    """Precio desde una fecha (USD por millón de tokens). Los anteriores se conservan: el coste
    de cada petición pasada sigue siendo el que fue."""
    since = body.effective_from or utcnow()
    if since.tzinfo is not None:
        since = since.replace(tzinfo=None) - since.utcoffset()
    async with deps_of(request).sessions() as db:
        model = await get_live(db, LlmModel, model_id, "el modelo")
        price = ModelPrice(model_id=model.id, effective_from=since, input_per_mtok=body.input_per_mtok,
                           output_per_mtok=body.output_per_mtok, cache_read_per_mtok=body.cache_read_per_mtok,
                           cache_write_per_mtok=body.cache_write_per_mtok, created_by=who.user_id)
        db.add(price)
        await flush(db, "Ya hay un precio de ese modelo desde esa fecha")
        record(db, request, who, "price.create", "model", model.id, {**body.model_dump(), "effective_from": since},
               organization_id=None)
        await commit(db, "Ya hay un precio de ese modelo desde esa fecha")
        return _price_body(price)


# --- Despliegues

@router.post("/deployments", status_code=201)
async def create_deployment(request: Request, body: DeploymentIn, who: Principal = Depends(operator)):
    async with deps_of(request).sessions() as db:
        model = await get_live(db, LlmModel, body.model_id, "el modelo")
        await get_live(db, Provider, body.provider_id, "el proveedor")
        deployment = Deployment(model_id=model.id, provider_id=body.provider_id,
                                upstream_model=body.upstream_model or model.model_key, priority=body.priority,
                                weight=body.weight)
        db.add(deployment)
        await flush(db)
        record(db, request, who, "deployment.create", "deployment", deployment.id, body.model_dump(),
               organization_id=None)
        await commit(db)
        return _deployment_body(deployment, set())


@router.patch("/deployments/{deployment_id}")
async def update_deployment(request: Request, deployment_id: int, body: DeploymentPatch,
                            who: Principal = Depends(operator)):
    changes = body.model_dump(exclude_unset=True)
    _no_nulls(changes)
    deps = deps_of(request)
    async with deps.sessions() as db:
        deployment = await get_live(db, Deployment, deployment_id, "el despliegue")
        for field, value in changes.items():
            setattr(deployment, field, value)
        record(db, request, who, "deployment.update", "deployment", deployment.id, changes, organization_id=None)
        await commit(db)
    return _deployment_body(deployment, await cooldown.cooled(deps.redis, [deployment.id]))


@router.delete("/deployments/{deployment_id}/cooldown", status_code=204)
async def clear_cooldown(request: Request, deployment_id: int, who: Principal = Depends(operator)):
    """Vuelve a poner el despliegue en su sitio de la cola (p. ej. tras arreglar la clave)."""
    deps = deps_of(request)
    async with deps.sessions() as db:
        await get_live(db, Deployment, deployment_id, "el despliegue")
        if deps.redis is None:
            raise NotFound("Sin Redis no hay enfriamiento")
        await cooldown.clear(deps.redis, deployment_id)
        record(db, request, who, "deployment.clear_cooldown", "deployment", deployment_id, organization_id=None)
        await commit(db)
    return Response(status_code=204)
