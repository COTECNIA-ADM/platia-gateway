"""Webhooks firmados desde el panel (G13; avisos de G12, ADR-016).

Un administrador gestiona los de su organización; los globales (todas las organizaciones y
los eventos del gateway), solo quien opera el gateway. El secreto de firma se enseña una
vez, al crearlo o al cambiarlo, y nunca vuelve a salir.
"""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from sqlalchemy import func, select, update

from .. import webhooks
from ..errors import InvalidRequest, NotFound, PermissionDenied, Unavailable
from ..identity import Principal
from ..models import DeliveryStatus, Webhook, WebhookDelivery
from ..models.base import utcnow
from ..pipeline.runner import deps_of
from .common import Body, admin, commit, flush, iso, record

router = APIRouter(tags=["panel: webhooks"])


class WebhookIn(Body):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(min_length=1, max_length=20)
    # Global: recibe todas las organizaciones y los eventos del gateway (solo operadores)
    global_: bool = Field(default=False, alias="global")


class WebhookPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    events: list[str] | None = Field(default=None, min_length=1, max_length=20)
    is_active: bool | None = None


def _check(request: Request, url: str | None = None, events: list[str] | None = None) -> list[str] | None:
    settings = deps_of(request).settings
    try:
        if url is not None:
            webhooks.check_url(url, settings.webhook_private_host_set)
        return webhooks.validate_events(events) if events is not None else None
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def _key(request: Request) -> bytes:
    deps = deps_of(request)
    if deps.encryption_key is None:
        raise Unavailable("Falta ENCRYPTION_KEY: el gateway no puede guardar ni leer secretos de firma")
    return deps.encryption_key


async def _hook(db, hook_id: int, who: Principal) -> Webhook:
    hook = await db.get(Webhook, hook_id)
    if hook is None or hook.deleted_at is not None:
        raise NotFound(f"No existe el webhook {hook_id}")
    if hook.organization_id is None and not who.platform_operator:
        raise NotFound(f"No existe el webhook {hook_id}")
    if hook.organization_id is not None and hook.organization_id != who.organization_id:
        raise NotFound(f"No existe el webhook {hook_id}")
    return hook


def _body(hook: Webhook, counts: dict | None = None) -> dict:
    counts = counts or {}
    return {"id": hook.id, "name": hook.name, "url": hook.url, "events": hook.events, "is_active": hook.is_active,
            "global": hook.organization_id is None, "consecutive_failures": hook.consecutive_failures,
            "disabled_reason": hook.disabled_reason, "created_at": iso(hook.created_at),
            "deliveries": {str(s): counts.get(s, 0) for s in DeliveryStatus}}


async def _counts(db, hook_id: int) -> dict:
    return dict((await db.execute(select(WebhookDelivery.status, func.count()).where(
        WebhookDelivery.webhook_id == hook_id).group_by(WebhookDelivery.status))).all())


@router.get("/webhooks")
async def list_webhooks(request: Request, who: Principal = Depends(admin)):
    scope = Webhook.organization_id == who.organization_id
    if who.platform_operator:
        scope = scope | Webhook.organization_id.is_(None)
    async with deps_of(request).sessions() as db:
        hooks = (await db.execute(select(Webhook).where(scope, Webhook.deleted_at.is_(None))
                                  .order_by(Webhook.id))).scalars().all()
        return {"data": [_body(h, await _counts(db, h.id)) for h in hooks], "events": list(webhooks.WEBHOOK_EVENTS)}


@router.post("/webhooks", status_code=201)
async def create_webhook(request: Request, body: WebhookIn, who: Principal = Depends(admin)):
    if body.global_ and not who.platform_operator:
        raise PermissionDenied("Solo quien opera el gateway puede crear webhooks globales")
    events = _check(request, body.url, body.events)
    key = _key(request)
    secret = webhooks.new_secret()
    async with deps_of(request).sessions() as db:
        hook = Webhook(organization_id=None if body.global_ else who.organization_id, name=body.name, url=body.url,
                       events=events, secret_encrypted="-")
        db.add(hook)
        await flush(db)
        hook.secret_encrypted = webhooks.seal_secret(secret, webhook_id=hook.id, key=key)
        record(db, request, who, "webhook.create", "webhook", hook.id,
               {"name": body.name, "url": body.url, "events": events, "global": body.global_, "secret": True},
               organization_id=hook.organization_id)
        await commit(db)
        return JSONResponse({**_body(hook), "secret": secret}, status_code=201, headers={"Cache-Control": "no-store"})


@router.patch("/webhooks/{hook_id}")
async def update_webhook(request: Request, hook_id: int, body: WebhookPatch, who: Principal = Depends(admin)):
    changes = body.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise InvalidRequest("Los campos no pueden ser nulos")
    events = _check(request, changes.get("url"), changes.get("events"))
    async with deps_of(request).sessions() as db:
        hook = await _hook(db, hook_id, who)
        if events is not None:
            changes["events"] = events
        for field, value in changes.items():
            setattr(hook, field, value)
        if changes.get("is_active") is True:
            hook.disabled_reason, hook.consecutive_failures = None, 0
        if changes.get("is_active") is False:
            # Lo pendiente ya no se envía
            await db.execute(update(WebhookDelivery).where(WebhookDelivery.webhook_id == hook.id,
                                                           WebhookDelivery.status == DeliveryStatus.PENDING)
                             .values(status=DeliveryStatus.CANCELLED, last_error="webhook_inactivo"))
        record(db, request, who, "webhook.update", "webhook", hook.id, changes, organization_id=hook.organization_id)
        await commit(db)
        return _body(hook, await _counts(db, hook.id))


@router.post("/webhooks/{hook_id}/rotate-secret")
async def rotate_secret(request: Request, hook_id: int, who: Principal = Depends(admin)):
    key = _key(request)
    secret = webhooks.new_secret()
    async with deps_of(request).sessions() as db:
        hook = await _hook(db, hook_id, who)
        hook.secret_encrypted = webhooks.seal_secret(secret, webhook_id=hook.id, key=key)
        record(db, request, who, "webhook.rotate_secret", "webhook", hook.id, {"secret": True},
               organization_id=hook.organization_id)
        await commit(db)
    return JSONResponse({"id": hook_id, "secret": secret}, headers={"Cache-Control": "no-store"})


@router.delete("/webhooks/{hook_id}", status_code=204)
async def delete_webhook(request: Request, hook_id: int, who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        hook = await _hook(db, hook_id, who)
        hook.is_active, hook.deleted_at = False, utcnow()
        await db.execute(update(WebhookDelivery).where(WebhookDelivery.webhook_id == hook.id,
                                                       WebhookDelivery.status == DeliveryStatus.PENDING)
                         .values(status=DeliveryStatus.CANCELLED, last_error="webhook_inactivo"))
        record(db, request, who, "webhook.delete", "webhook", hook.id, organization_id=hook.organization_id)
        await commit(db)
    return Response(status_code=204)


@router.post("/webhooks/{hook_id}/test")
async def test_webhook(request: Request, hook_id: int, who: Principal = Depends(admin)):
    """Envía ahora un `webhook.test` y dice qué contestó el receptor (solo el código)."""
    deps = deps_of(request)
    _key(request)
    async with deps.sessions() as db:
        await _hook(db, hook_id, who)
    result = await webhooks.send_test(deps, request.app.state.webhook_http, hook_id)
    return {"ok": result.ok, "status_code": result.status_code, "error": result.error,
            "duration_ms": result.duration_ms}


@router.get("/webhooks/{hook_id}/deliveries")
async def list_deliveries(request: Request, hook_id: int, limit: int = Query(50, ge=1, le=200),
                          who: Principal = Depends(admin)):
    async with deps_of(request).sessions() as db:
        await _hook(db, hook_id, who)
        rows = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id)
                                 .order_by(WebhookDelivery.id.desc()).limit(limit))).scalars()
        return {"data": [{"id": d.id, "event": d.event, "event_id": d.event_id, "status": str(d.status),
                          "attempts": d.attempts, "next_attempt_at": iso(d.next_attempt_at),
                          "last_status_code": d.last_status_code, "last_error": d.last_error,
                          "last_duration_ms": d.last_duration_ms, "created_at": iso(d.created_at),
                          "delivered_at": iso(d.delivered_at)} for d in rows]}

