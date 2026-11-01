"""Alta de la configuración mínima desde la línea de comandos, hasta que llegue el panel (G13).

Uso dentro de Docker (el contenedor de migraciones tiene acceso a MySQL):

    docker compose run --rm migrations python -m app.cli <orden> [opciones]

Órdenes: create-org, create-team, create-app, create-key, revoke-key, create-provider,
create-model, set-fallbacks (cadena de reserva), create-deployment, set-deployment
(prioridad, peso o estado), set-price, set-limit, remove-limit y limits (cuotas y límites de
ritmo, G7), set-content-policy y content-policies (seguridad de contenido, G8),
audit-seal, audit-verify, audit-export y audit-pseudonymize (auditoría verificable y RGPD,
G9), show y usage (consumo y coste, para compararlo con lo que registra PLATIA).
`python -m app.cli <orden> -h` explica cada una.

La clave de un proveedor nunca va como argumento (quedaría en el historial de la shell):
se lee de una variable de entorno (`--api-key-env`) o de la entrada estándar. La clave de
una aplicación se enseña una sola vez, al crearla.
"""
import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import case, create_engine, func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .crypto import encrypt, load_key
from .keys import new_application_key
from . import audit_chain
from .content_policy import ALLOWED, DEFAULTS
from .limits import window
from .models import (ApiKey, Application, ApplicationStatus, AuditLog, AuditStatus, ContentAction, ContentCheck,
                     ContentPolicy, Deployment, Direction, KeyKind, PolicyScope,
                     LimitMetric, LimitPeriod, LimitScope, LlmModel, ModelFamily, ModelPrice, Organization, Provider,
                     ProviderKind, Team, UsageLimit)
from .models.base import utcnow
from .settings import Settings


class CliError(Exception):
    pass


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _one(db: Session, model, **filters):
    row = db.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if row is None:
        described = ", ".join(f"{k}={v}" for k, v in filters.items())
        raise CliError(f"No existe {model.__tablename__} con {described}")
    return row


def _org(db: Session, slug: str) -> Organization:
    return _one(db, Organization, slug=slug, deleted_at=None)


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"no es un número: {value}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("no puede ser negativo")
    return number


def _read_secret(env_name: str | None) -> str:
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            raise CliError(f"La variable de entorno {env_name} está vacía")
        return value.strip()
    if sys.stdin.isatty():
        return getpass.getpass("Clave API del proveedor (no se muestra): ").strip()
    return sys.stdin.readline().strip()


# --- Órdenes

def create_org(db: Session, args, settings: Settings) -> None:
    org = Organization(slug=args.slug, name=args.name,
                       allowed_providers=args.providers.split(",") if args.providers else None)
    db.add(org)
    db.flush()
    _out(f"Organización '{org.slug}' creada (id {org.id})")


def create_team(db: Session, args, settings: Settings) -> None:
    org = _org(db, args.org)
    team = Team(organization_id=org.id, slug=args.slug, name=args.name)
    db.add(team)
    db.flush()
    _out(f"Equipo '{team.slug}' creado en '{org.slug}' (id {team.id})")


def create_app(db: Session, args, settings: Settings) -> None:
    org = _org(db, args.org)
    team = _one(db, Team, organization_id=org.id, slug=args.team, deleted_at=None)
    app = Application(organization_id=org.id, team_id=team.id, slug=args.slug, name=args.name,
                      trusted=args.trusted, allowed_purposes=args.purpose or [],
                      allowed_models=args.model or None)
    db.add(app)
    db.flush()
    _out(f"Aplicación '{app.slug}' creada (id {app.id}, de confianza: {'sí' if app.trusted else 'no'})")


def create_key(db: Session, args, settings: Settings) -> None:
    org = _org(db, args.org)
    app = _one(db, Application, organization_id=org.id, slug=args.app, deleted_at=None)
    if app.status != ApplicationStatus.ACTIVE:
        raise CliError(f"La aplicación '{app.slug}' no está activa")
    new = new_application_key(app.slug)
    expires = utcnow() + timedelta(days=args.expires_days) if args.expires_days else None
    db.add(ApiKey(organization_id=org.id, kind=KeyKind.APPLICATION, application_id=app.id, prefix=new.prefix,
                  key_hash=new.key_hash, label=args.label, expires_at=expires))
    db.flush()
    _out(f"Clave creada para '{app.slug}' (prefijo {new.prefix}). Guárdala ahora: no se volverá a mostrar.")
    _out(new.raw)


def revoke_key(db: Session, args, settings: Settings) -> None:
    keys = db.execute(select(ApiKey).where(ApiKey.prefix == args.prefix, ApiKey.revoked_at.is_(None))).scalars().all()
    if len(keys) != 1:
        raise CliError(f"El prefijo {args.prefix} corresponde a {len(keys)} claves activas; hace falta exactamente 1")
    keys[0].revoked_at = utcnow()
    _out(f"Clave {args.prefix} revocada")


def create_provider(db: Session, args, settings: Settings) -> None:
    base_url = args.base_url
    if args.kind == ProviderKind.OPENAI_COMPATIBLE and not base_url:
        raise CliError("Un proveedor compatible con OpenAI necesita --base-url (p. ej. https://api.openai.com/v1)")
    if base_url and not base_url.startswith("https://") and not args.allow_http:
        raise CliError("--base-url debe ser https:// (usa --allow-http solo para un servicio de la red interna)")
    encrypted = None
    if not args.no_api_key:
        if not settings.encryption_key:
            raise CliError("Falta ENCRYPTION_KEY: no se puede guardar la clave del proveedor")
        secret = _read_secret(args.api_key_env)
        if not secret:
            raise CliError("La clave del proveedor está vacía")
        encrypted = encrypt(secret, key=load_key(settings.encryption_key), context=f"provider:{args.slug}")
    provider = Provider(slug=args.slug, name=args.name, kind=args.kind, base_url=base_url,
                        api_key_encrypted=encrypted)
    db.add(provider)
    db.flush()
    _out(f"Proveedor '{provider.slug}' creado (id {provider.id}, clave {'cifrada' if encrypted else 'sin clave'})")


def create_model(db: Session, args, settings: Settings) -> None:
    model = LlmModel(model_key=args.key, display_name=args.name, family=args.family,
                     context_window=args.context_window, max_output_tokens=args.max_output_tokens,
                     capabilities=args.capability or [],
                     fallback_models=_fallbacks(db, args.key, args.fallback) or None)
    db.add(model)
    db.flush()
    _out(f"Modelo '{model.model_key}' creado (id {model.id})")


def _fallbacks(db: Session, model_key: str, keys: list[str] | None) -> list[str]:
    """Comprueba la cadena de reserva: modelos existentes, sin repetir y sin el propio modelo."""
    chain: list[str] = []
    for key in keys or []:
        if key == model_key:
            raise CliError("Un modelo no puede ser su propia reserva")
        if key not in chain:
            _one(db, LlmModel, model_key=key, deleted_at=None)
            chain.append(key)
    return chain


def set_fallbacks(db: Session, args, settings: Settings) -> None:
    model = _one(db, LlmModel, model_key=args.model, deleted_at=None)
    model.fallback_models = _fallbacks(db, model.model_key, args.to) or None
    db.flush()
    _out(f"Reserva de '{model.model_key}': {' → '.join(model.fallback_models or []) or 'ninguna'}")


def set_deployment(db: Session, args, settings: Settings) -> None:
    deployment = _one(db, Deployment, id=args.id, deleted_at=None)
    if args.priority is not None:
        deployment.priority = args.priority
    if args.weight is not None:
        deployment.weight = args.weight
    if args.active is not None:
        deployment.is_active = args.active
    db.flush()
    _out(f"Despliegue {deployment.id}: prioridad {deployment.priority}, peso {deployment.weight}, "
         f"{'activo' if deployment.is_active else 'inactivo'}")


def create_deployment(db: Session, args, settings: Settings) -> None:
    model = _one(db, LlmModel, model_key=args.model, deleted_at=None)
    provider = _one(db, Provider, slug=args.provider, deleted_at=None)
    deployment = Deployment(model_id=model.id, provider_id=provider.id, upstream_model=args.upstream or model.model_key,
                            priority=args.priority, weight=args.weight)
    db.add(deployment)
    db.flush()
    _out(f"Despliegue {deployment.id}: '{model.model_key}' en '{provider.slug}' como '{deployment.upstream_model}'")


def set_price(db: Session, args, settings: Settings) -> None:
    model = _one(db, LlmModel, model_key=args.model, deleted_at=None)
    since = datetime.fromisoformat(args.since) if args.since else utcnow()
    db.add(ModelPrice(model_id=model.id, effective_from=since, input_per_mtok=args.input,
                      output_per_mtok=args.output, cache_read_per_mtok=args.cache_read,
                      cache_write_per_mtok=args.cache_write))
    db.flush()
    _out(f"Precio de '{model.model_key}' desde {since:%Y-%m-%d %H:%M}: {args.input} / {args.output} USD por Mtok")


def _limit_scope(db: Session, args) -> tuple[LimitScope, int, str | None, str]:
    """Ámbito del límite según las opciones: --user (con --app), --app, --team o solo --org."""
    org = _org(db, args.org)
    if args.user is not None and not args.app:
        raise CliError("--user necesita --app: la persona es la que declara esa aplicación")
    if args.app and args.team:
        raise CliError("Usa --team o --app, no los dos")
    if args.app:
        app = _one(db, Application, organization_id=org.id, slug=args.app, deleted_at=None)
        if args.user is not None:
            subject = args.user or None  # --user '' = cada persona de la aplicación
            label = f"persona {subject} de {app.slug}" if subject else f"cada persona de {app.slug}"
            return LimitScope.END_USER, app.id, subject, label
        return LimitScope.APPLICATION, app.id, None, f"aplicación {app.slug}"
    if args.team:
        team = _one(db, Team, organization_id=org.id, slug=args.team, deleted_at=None)
        return LimitScope.TEAM, team.id, None, f"equipo {team.slug}"
    return LimitScope.ORGANIZATION, org.id, None, f"organización {org.slug}"


def set_limit(db: Session, args, settings: Settings) -> None:
    metric = args.metric
    if metric == LimitMetric.CONCURRENT:
        period = LimitPeriod.NONE
    elif args.period is None or args.period == LimitPeriod.NONE:
        raise CliError("Falta --period (minute, day o month)")
    elif metric == LimitMetric.COST_USD and args.period == LimitPeriod.MINUTE:
        raise CliError("El gasto se limita por día o por mes")
    else:
        period = args.period
    if not 1 <= args.alert_pct <= 100:
        raise CliError("--alert-pct va de 1 a 100")
    scope, scope_id, subject, label = _limit_scope(db, args)
    existing = db.execute(select(UsageLimit).where(
        UsageLimit.scope == scope, UsageLimit.scope_id == scope_id, UsageLimit.metric == metric,
        UsageLimit.period == period, UsageLimit.deleted_at.is_(None),
        UsageLimit.subject.is_(None) if subject is None else UsageLimit.subject == subject)).scalars().first()
    if existing is None:
        existing = UsageLimit(scope=scope, scope_id=scope_id, subject=subject, metric=metric, period=period,
                              limit_value=args.value, alert_pct=args.alert_pct)
        db.add(existing)
        verb = "creado"
    else:
        existing.limit_value, existing.alert_pct, existing.is_active = args.value, args.alert_pct, True
        verb = "actualizado"
    db.flush()
    _out(f"Límite {existing.id} {verb}: {label}, {metric} {period} = {args.value} (aviso al {args.alert_pct} %)")


def remove_limit(db: Session, args, settings: Settings) -> None:
    limit = _one(db, UsageLimit, id=args.id, deleted_at=None)
    limit.deleted_at, limit.is_active = utcnow(), False
    _out(f"Límite {limit.id} eliminado")


def _used(db: Session, limit: UsageLimit) -> Decimal | None:
    """Lo consumido en el periodo actual según la auditoría (día y mes; minuto y simultáneas, no)."""
    if limit.period not in (LimitPeriod.DAY, LimitPeriod.MONTH):
        return None
    column = {LimitScope.ORGANIZATION: AuditLog.organization_id, LimitScope.TEAM: AuditLog.team_id}.get(
        limit.scope, AuditLog.application_id)
    where = [column == limit.scope_id, AuditLog.created_at >= window(limit.period, utcnow()).start,
             AuditLog.status != AuditStatus.BLOCKED]
    if limit.scope == LimitScope.END_USER and limit.subject:
        where.append(AuditLog.end_user_ref == limit.subject)
    measure = {LimitMetric.COST_USD: func.sum(AuditLog.cost_usd), LimitMetric.REQUESTS: func.count(),
               LimitMetric.TOKENS: func.sum(AuditLog.input_tokens + AuditLog.output_tokens
                                            + AuditLog.cache_read_tokens + AuditLog.cache_write_tokens)}
    return Decimal(db.execute(select(measure[limit.metric]).where(*where)).scalar() or 0)


def limits(db: Session, args, settings: Settings) -> None:
    names = {LimitScope.ORGANIZATION: (Organization, "organización"), LimitScope.TEAM: (Team, "equipo"),
             LimitScope.APPLICATION: (Application, "aplicación"), LimitScope.END_USER: (Application, "personas de")}
    rank = list(LimitScope)  # de lo más amplio (organización) a lo más concreto (persona)
    rows = sorted(db.execute(select(UsageLimit).where(UsageLimit.deleted_at.is_(None))).scalars(),
                  key=lambda r: (rank.index(r.scope), r.scope_id, r.id))
    if not rows:
        _out("No hay límites: el gateway no limita el gasto ni el ritmo")
        return
    for limit in rows:
        model, word = names[limit.scope]
        owner = db.get(model, limit.scope_id)
        who = f"{word} {owner.slug if owner else limit.scope_id}"
        if limit.subject:
            who = f"persona {limit.subject} de {owner.slug if owner else limit.scope_id}"
        used = _used(db, limit)
        # En las personas generales, el consumo es de todas juntas: solo orientativo
        shown = "" if used is None or (limit.scope == LimitScope.END_USER and not limit.subject) else \
            f" · usado {used:.6f}" if limit.metric == LimitMetric.COST_USD else f" · usado {int(used)}"
        state = "" if limit.is_active else " (inactivo)"
        _out(f"[{limit.id}] {who}: {limit.metric} {limit.period} ≤ {limit.limit_value.normalize():f}"
             f" (aviso {limit.alert_pct} %){shown}{state}")


def set_content_policy(db: Session, args, settings: Settings) -> None:
    org = _org(db, args.org)
    key = (args.direction, args.check)
    if key not in ALLOWED:
        raise CliError(f"En la {'salida' if args.direction == Direction.OUTPUT else 'entrada'} no se comprueba "
                       f"'{args.check}'")
    if args.action != "default" and ContentAction(args.action) not in ALLOWED[key]:
        valid = ", ".join(sorted(ALLOWED[key]))
        raise CliError(f"'{args.action}' no vale para {args.check}: usa {valid} o default")
    if args.app:
        app = _one(db, Application, organization_id=org.id, slug=args.app, deleted_at=None)
        scope, scope_id, label = PolicyScope.APPLICATION, app.id, f"aplicación {app.slug}"
    else:
        scope, scope_id, label = PolicyScope.ORGANIZATION, org.id, f"organización {org.slug}"
    row = db.execute(select(ContentPolicy).filter_by(scope=scope, scope_id=scope_id, direction=args.direction,
                                                     check_name=args.check)).scalar_one_or_none()
    if args.action == "default":
        if row is not None and row.deleted_at is None:
            row.deleted_at = utcnow()
        _out(f"{label}: {args.direction}.{args.check} vuelve a lo heredado")
        return
    if row is None:
        db.add(ContentPolicy(scope=scope, scope_id=scope_id, direction=args.direction, check_name=args.check,
                             action=args.action))
    else:  # se reutiliza la fila (también si estaba borrada): una sola por casilla
        row.action, row.deleted_at = args.action, None
    db.flush()
    _out(f"{label}: {args.direction}.{args.check} = {args.action}")


def content_policies(db: Session, args, settings: Settings) -> None:
    """La política efectiva de una organización (o de una de sus aplicaciones) y de dónde sale cada casilla."""
    org = _org(db, args.org)
    scopes = [(PolicyScope.ORGANIZATION, org.id, "organización")]
    if args.app:
        app = _one(db, Application, organization_id=org.id, slug=args.app, deleted_at=None)
        scopes.append((PolicyScope.APPLICATION, app.id, "aplicación"))
    effective = {key: (action, "por defecto") for key, action in DEFAULTS.items()}
    for scope, scope_id, origin in scopes:
        for row in db.execute(select(ContentPolicy).filter_by(scope=scope, scope_id=scope_id, deleted_at=None)):
            row = row[0]
            effective[(row.direction, row.check_name)] = (row.action, origin)
    for (direction, check), (action, origin) in effective.items():
        _out(f"{direction:<6} {check:<10} {action:<6} ({origin})")


def _run_async(settings: Settings, work):
    """Las órdenes de la cadena usan el mismo código asíncrono que el gateway."""
    async def main():
        engine = create_async_engine(settings.database_url)
        try:
            return await work(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()
    return asyncio.run(main())


def audit_seal(db: Session, args, settings: Settings) -> None:
    async def work(sessions):
        total = 0
        while (sealed := await audit_chain.seal(sessions, settings.audit_pending_max_seconds)) > 0:
            total += sealed
        return total
    _out(f"Filas selladas: {_run_async(settings, work)}")


def audit_verify(db: Session, args, settings: Settings) -> None:
    result = _run_async(settings, audit_chain.verify)
    if not result.ok:
        raise CliError(f"{result.message} (fila {result.broken_at}; comprobadas {result.checked} hasta la "
                       f"{result.last_id})")
    _out(f"{result.message}: {result.checked} filas selladas comprobadas, hasta la {result.last_id}")


def _person_digest(org: Organization, ref: str) -> str:
    return audit_chain.keyed_digest(org.audit_salt, ref)


def audit_export(db: Session, args, settings: Settings) -> None:
    """Filas de la auditoría en JSON (una por línea), con su hash, para un auditor o una
    petición de acceso (RGPD). Nunca hay texto de la conversación: no se guarda."""
    org = _org(db, args.org)
    where = [AuditLog.organization_id == org.id]
    if args.app:
        where.append(AuditLog.application_id == _one(db, Application, organization_id=org.id, slug=args.app,
                                                     deleted_at=None).id)
    if args.user:
        # Por la huella (sirve también tras seudonimizar) o, en filas anteriores a G9, por la referencia
        where.append(or_(AuditLog.end_user_digest == _person_digest(org, args.user),
                         AuditLog.end_user_ref == args.user))
    if args.since:
        where.append(AuditLog.created_at >= datetime.fromisoformat(args.since))
    if args.until:
        where.append(AuditLog.created_at < datetime.fromisoformat(args.until))
    count = 0
    for row in db.execute(select(AuditLog).where(*where).order_by(AuditLog.id)).scalars():
        data = json.loads(audit_chain.row_payload(row))
        data["chain_hash"] = row.chain_hash
        if not args.pseudonymize:
            data["end_user_ref"] = row.end_user_ref
        _out(json.dumps(data, ensure_ascii=False, sort_keys=True))
        count += 1
    sys.stderr.write(f"Exportadas {count} filas\n")


def audit_pseudonymize(db: Session, args, settings: Settings) -> None:
    """Sustituye la referencia de una persona por un seudónimo en toda la auditoría (RGPD).

    La cadena no se rompe: cubre la huella, no la referencia. Con la sal de la organización
    el seudónimo aún se puede relacionar con la persona (es seudonimización, no anonimización).
    """
    org = _org(db, args.org)
    pseudonym = "seud:" + _person_digest(org, args.user)[:24]
    result = db.execute(update(AuditLog).where(AuditLog.organization_id == org.id,
                                               AuditLog.end_user_ref == args.user).values(end_user_ref=pseudonym))
    _out(f"Persona {args.user!r} seudonimizada como {pseudonym} en {result.rowcount} filas")


def show(db: Session, args, settings: Settings) -> None:
    for org in db.execute(select(Organization).where(Organization.deleted_at.is_(None))).scalars():
        _out(f"Organización {org.slug} ({'activa' if org.is_active else 'inactiva'})")
        apps = db.execute(select(Application).where(Application.organization_id == org.id,
                                                    Application.deleted_at.is_(None))).scalars()
        for app in apps:
            keys = db.execute(select(ApiKey).where(ApiKey.application_id == app.id,
                                                   ApiKey.revoked_at.is_(None))).scalars().all()
            prefixes = ", ".join(k.prefix for k in keys) or "sin claves"
            _out(f"  Aplicación {app.slug} [{app.status}] propósitos={app.allowed_purposes} claves: {prefixes}")
    rows = db.execute(select(Deployment, LlmModel, Provider).join(LlmModel, Deployment.model_id == LlmModel.id)
                      .join(Provider, Deployment.provider_id == Provider.id)
                      .where(Deployment.deleted_at.is_(None))).all()
    for deployment, model, provider in rows:
        state = "" if deployment.is_active else ", inactivo"
        _out(f"Modelo {model.model_key} → {provider.slug} ({provider.kind}) como {deployment.upstream_model} "
             f"[despliegue {deployment.id}, prioridad {deployment.priority}, peso {deployment.weight}{state}]")
    for model in db.execute(select(LlmModel).where(LlmModel.deleted_at.is_(None),
                                                   LlmModel.fallback_models.is_not(None))).scalars():
        if model.fallback_models:
            _out(f"Reserva de {model.model_key}: {' → '.join(model.fallback_models)}")


def usage(db: Session, args, settings: Settings) -> None:
    """Llamadas, tokens y coste por propósito y modelo, a partir de la auditoría."""
    filters = []
    if args.app:
        app_ids = [a.id for a in db.execute(select(Application).where(Application.slug == args.app)).scalars()]
        if not app_ids:
            raise CliError(f"No existe ninguna aplicación '{args.app}'")
        filters.append(AuditLog.application_id.in_(app_ids))
    if args.since:
        filters.append(AuditLog.created_at >= datetime.fromisoformat(args.since))
    if args.until:
        filters.append(AuditLog.created_at < datetime.fromisoformat(args.until))
    if args.request_id:
        filters.append(AuditLog.request_id == args.request_id)
    rows = db.execute(
        select(AuditLog.purpose, AuditLog.model_key, func.count(),
               func.sum(case((AuditLog.status == AuditStatus.SUCCESS, 1), else_=0)),
               func.sum(AuditLog.input_tokens), func.sum(AuditLog.output_tokens),
               func.sum(AuditLog.cache_read_tokens), func.sum(AuditLog.cache_write_tokens), func.sum(AuditLog.cost_usd))
        .where(*filters).group_by(AuditLog.purpose, AuditLog.model_key)
        .order_by(AuditLog.purpose, AuditLog.model_key)
    ).all()
    _out(f"{'propósito':<24} {'modelo':<22} {'llamadas':>8} {'ok':>6} {'entrada':>10} {'salida':>10} "
         f"{'caché lee':>10} {'caché esc':>10} {'USD':>12}")
    total = Decimal("0")
    for purpose, model, calls, ok, tin, tout, cread, cwrite, cost in rows:
        cost = Decimal(cost or 0)
        total += cost
        _out(f"{(purpose or '-')[:24]:<24} {(model or '-')[:22]:<22} {calls:>8} {int(ok or 0):>6} {int(tin or 0):>10} "
             f"{int(tout or 0):>10} {int(cread or 0):>10} {int(cwrite or 0):>10} {cost:>12.6f}")
    _out(f"Total: {total:.6f} USD en {sum(r[2] for r in rows)} llamadas")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Configuración del PLATIA Gateway")
    parser.add_argument("--exists-ok", action="store_true",
                        help="Si lo que se crea ya existe, no es un error (para guiones que se repiten)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-org", help="Crear una organización")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--providers", help="Proveedores permitidos, separados por comas (por defecto, todos)")
    p.set_defaults(handler=create_org)

    p = sub.add_parser("create-team", help="Crear un equipo")
    p.add_argument("--org", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(handler=create_team)

    p = sub.add_parser("create-app", help="Registrar una aplicación cliente")
    p.add_argument("--org", required=True)
    p.add_argument("--team", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--trusted", action="store_true", help="Puede declarar la persona final (X-Platia-User-Id)")
    p.add_argument("--purpose", action="append", help="Propósito permitido (repetible); 'default' siempre lo está y '*' permite cualquiera")
    p.add_argument("--model", action="append", help="Modelo permitido (repetible); sin él, todos")
    p.set_defaults(handler=create_app)

    p = sub.add_parser("create-key", help="Crear una clave para una aplicación (se muestra una vez)")
    p.add_argument("--org", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--expires-days", type=int, help="Caducidad, para rotar sin cortes")
    p.set_defaults(handler=create_key)

    p = sub.add_parser("revoke-key", help="Revocar una clave por su prefijo")
    p.add_argument("--prefix", required=True)
    p.set_defaults(handler=revoke_key)

    p = sub.add_parser("create-provider", help="Registrar una cuenta de proveedor (la clave se guarda cifrada)")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--kind", required=True, type=ProviderKind, choices=list(ProviderKind))
    p.add_argument("--base-url", help="Por defecto, la de Anthropic; obligatoria en compatibles con OpenAI")
    p.add_argument("--api-key-env", help="Variable de entorno con la clave (si no, se lee de la entrada)")
    p.add_argument("--no-api-key", action="store_true", help="Proveedor sin clave (servidor interno)")
    p.add_argument("--allow-http", action="store_true", help="Permitir http:// (solo red interna)")
    p.set_defaults(handler=create_provider)

    p = sub.add_parser("create-model", help="Dar de alta un modelo del catálogo")
    p.add_argument("--key", required=True, help="Nombre que piden los clientes, p. ej. claude-sonnet-5-5")
    p.add_argument("--name", required=True)
    p.add_argument("--family", required=True, type=ModelFamily, choices=list(ModelFamily))
    p.add_argument("--context-window", type=int)
    p.add_argument("--max-output-tokens", type=int)
    p.add_argument("--capability", action="append", help="tools, thinking, structured_output... (repetible)")
    p.add_argument("--fallback", action="append", help="Modelo de reserva, en orden (repetible)")
    p.set_defaults(handler=create_model)

    p = sub.add_parser("set-fallbacks", help="Cambiar la cadena de reserva de un modelo")
    p.add_argument("--model", required=True)
    p.add_argument("--to", action="append", help="Modelo de reserva, en orden (repetible; sin --to, ninguna)")
    p.set_defaults(handler=set_fallbacks)

    p = sub.add_parser("set-deployment", help="Cambiar prioridad, peso o estado de un despliegue (id en 'show')")
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--priority", type=int)
    p.add_argument("--weight", type=int)
    state = p.add_mutually_exclusive_group()
    state.add_argument("--active", dest="active", action="store_const", const=True)
    state.add_argument("--inactive", dest="active", action="store_const", const=False)
    p.set_defaults(handler=set_deployment)

    p = sub.add_parser("create-deployment", help="Servir un modelo desde un proveedor")
    p.add_argument("--model", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--upstream", help="Nombre del modelo en el proveedor (por defecto, el mismo)")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--weight", type=int, default=100)
    p.set_defaults(handler=create_deployment)

    p = sub.add_parser("set-price", help="Precio de un modelo desde una fecha (USD por millón de tokens)")
    p.add_argument("--model", required=True)
    p.add_argument("--input", required=True, type=_decimal)
    p.add_argument("--output", required=True, type=_decimal)
    p.add_argument("--cache-read", type=_decimal)
    p.add_argument("--cache-write", type=_decimal)
    p.add_argument("--since", help="Fecha ISO en UTC (por defecto, ahora)")
    p.set_defaults(handler=set_price)

    p = sub.add_parser("set-limit", help="Crear o cambiar una cuota o un límite de ritmo (G7)")
    p.add_argument("--org", required=True)
    p.add_argument("--team", help="Límite del equipo")
    p.add_argument("--app", help="Límite de la aplicación")
    p.add_argument("--user", help="Con --app: límite de esa persona (X-Platia-User-Id); '' para cada persona")
    p.add_argument("--metric", required=True, type=LimitMetric, choices=list(LimitMetric),
                   help="cost_usd (USD), requests, tokens o concurrent (peticiones simultáneas)")
    p.add_argument("--period", type=LimitPeriod, choices=[LimitPeriod.MINUTE, LimitPeriod.DAY, LimitPeriod.MONTH],
                   help="minute, day o month (UTC); concurrent no lleva")
    p.add_argument("--value", required=True, type=_decimal)
    p.add_argument("--alert-pct", type=int, default=80, help="Aviso en el log al pasar este porcentaje")
    p.set_defaults(handler=set_limit)

    p = sub.add_parser("remove-limit", help="Quitar un límite (id en 'limits')")
    p.add_argument("--id", required=True, type=int)
    p.set_defaults(handler=remove_limit)

    p = sub.add_parser("limits", help="Límites configurados y consumo del periodo actual")
    p.set_defaults(handler=limits)

    p = sub.add_parser("set-content-policy", help="Qué hacer con un tipo de contenido (G8)")
    p.add_argument("--org", required=True)
    p.add_argument("--app", help="Solo para esta aplicación (manda sobre la organización)")
    p.add_argument("--direction", required=True, type=Direction, choices=list(Direction))
    p.add_argument("--check", required=True, type=ContentCheck, choices=list(ContentCheck))
    p.add_argument("--action", required=True, choices=[*ContentAction, "default"],
                   help="block, redact, log, off, o default para volver a lo heredado")
    p.set_defaults(handler=set_content_policy)

    p = sub.add_parser("content-policies", help="Política de contenido efectiva y de dónde sale")
    p.add_argument("--org", required=True)
    p.add_argument("--app")
    p.set_defaults(handler=content_policies)

    p = sub.add_parser("audit-seal", help="Sellar ahora las filas cerradas de la auditoría (G9)")
    p.set_defaults(handler=audit_seal)

    p = sub.add_parser("audit-verify", help="Comprobar que la cadena de la auditoría está íntegra")
    p.set_defaults(handler=audit_verify)

    p = sub.add_parser("audit-export", help="Exportar la auditoría en JSON, una fila por línea")
    p.add_argument("--org", required=True)
    p.add_argument("--app")
    p.add_argument("--user", help="Solo las filas de esta persona (X-Platia-User-Id), también seudonimizadas")
    p.add_argument("--since", help="Desde (fecha ISO en UTC, incluida)")
    p.add_argument("--until", help="Hasta (fecha ISO en UTC, excluida)")
    p.add_argument("--pseudonymize", action="store_true", help="Sin la referencia de la persona, solo su huella")
    p.set_defaults(handler=audit_export)

    p = sub.add_parser("audit-pseudonymize", help="Seudonimizar a una persona en la auditoría (RGPD)")
    p.add_argument("--org", required=True)
    p.add_argument("--user", required=True, help="Su X-Platia-User-Id")
    p.set_defaults(handler=audit_pseudonymize)

    p = sub.add_parser("show", help="Resumen de organizaciones, aplicaciones y modelos")
    p.set_defaults(handler=show)

    p = sub.add_parser("usage", help="Llamadas, tokens y coste por propósito y modelo (auditoría)")
    p.add_argument("--app", help="Slug de la aplicación (p. ej. platia)")
    p.add_argument("--since", help="Desde (fecha ISO en UTC, incluida)")
    p.add_argument("--until", help="Hasta (fecha ISO en UTC, excluida)")
    p.add_argument("--request-id", help="Solo una petición o tarea (en PLATIA, su correlation_id)")
    p.set_defaults(handler=usage)
    return parser


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or Settings()
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as db:
            args.handler(db, args, settings)
            db.commit()
    except CliError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except IntegrityError:
        if args.exists_ok:
            _out("Ya existía: se deja como estaba")
            return 0
        sys.stderr.write("Error: ya existe un registro con esos datos (slug, clave o fecha repetidos)\n")
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
