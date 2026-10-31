"""Alta de la configuración mínima desde la línea de comandos, hasta que llegue el panel (G13).

Uso dentro de Docker (el contenedor de migraciones tiene acceso a MySQL):

    docker compose run --rm migrations python -m app.cli <orden> [opciones]

Órdenes: create-org, create-team, create-app, create-key, revoke-key, create-provider,
create-model, create-deployment, set-price, show y usage (consumo y coste, para compararlo
con lo que registra PLATIA). `python -m app.cli <orden> -h`
explica cada una.

La clave de un proveedor nunca va como argumento (quedaría en el historial de la shell):
se lee de una variable de entorno (`--api-key-env`) o de la entrada estándar. La clave de
una aplicación se enseña una sola vez, al crearla.
"""
import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import case, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .crypto import encrypt, load_key
from .keys import new_application_key
from .models import (ApiKey, Application, ApplicationStatus, AuditLog, AuditStatus, Deployment, KeyKind, LlmModel, ModelFamily, ModelPrice,
                     Organization, Provider, ProviderKind, Team)
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
                     capabilities=args.capability or [])
    db.add(model)
    db.flush()
    _out(f"Modelo '{model.model_key}' creado (id {model.id})")


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
        _out(f"Modelo {model.model_key} → {provider.slug} ({provider.kind}) como {deployment.upstream_model} "
             f"[prioridad {deployment.priority}, peso {deployment.weight}]")


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
    p.set_defaults(handler=create_model)

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
