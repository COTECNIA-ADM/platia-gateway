"""Migraciones y restricciones contra un MySQL 8 real.

Necesitan `TEST_DATABASE_URL` (driver síncrono), por ejemplo
`mysql+pymysql://gateway:pass@127.0.0.1:3307/platia_gateway`. La base se vacía y se
recrea con Alembic: no apuntes nunca a una base con datos. La CI siempre la define.
"""
import hashlib
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models import (ApiKey, Application, AuditLog, Base, Contract, Deployment, KeyKind, LlmModel,
                        ModelFamily, ModelPrice, Organization, Provider, ProviderKind, Team, User)

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL no definida (la CI la define)")

GATEWAY_DIR = Path(__file__).resolve().parent.parent
TABLES = {"organizations", "teams", "users", "applications", "api_keys", "providers", "llm_models",
          "deployments", "model_prices", "audit_logs"}


def _alembic(connection) -> Config:
    cfg = Config(str(GATEWAY_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(GATEWAY_DIR / "migrations"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL)
    with eng.begin() as conn:
        command.downgrade(_alembic(conn), "base")
        command.upgrade(_alembic(conn), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


def _org(session, slug="cotecnia") -> Organization:
    org = Organization(slug=slug, name="Cotecnia")
    session.add(org)
    session.flush()
    return org


def _app(session, org) -> Application:
    team = Team(organization_id=org.id, slug="plataforma", name="Plataforma")
    session.add(team)
    session.flush()
    app = Application(organization_id=org.id, team_id=team.id, slug="platia", name="PLATIA", trusted=True,
                      allowed_purposes=["proposal_writer"])
    session.add(app)
    session.flush()
    return app


def _key(org, app=None, user=None, kind=KeyKind.APPLICATION, raw="plgw-app-platia-x") -> ApiKey:
    return ApiKey(organization_id=org.id, kind=kind, application_id=app.id if app else None,
                  user_id=user.id if user else None, prefix=raw[:20],
                  key_hash=hashlib.sha256(raw.encode()).hexdigest())


def test_migracion_baja_y_sube_y_coincide_con_los_modelos(engine):
    with engine.begin() as conn:
        command.downgrade(_alembic(conn), "base")
        assert not TABLES & set(inspect(conn).get_table_names())
        command.upgrade(_alembic(conn), "head")
        assert TABLES <= set(inspect(conn).get_table_names())
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        assert diff == []


def test_identidad_y_valores_por_defecto(session):
    org = _org(session)
    other = _org(session, "otra")
    app = _app(session, org)
    session.add(_key(org, app))
    session.flush()
    assert len(org.audit_salt) == 64 and org.audit_salt != other.audit_salt
    assert org.created_at is not None and org.is_active
    assert app.status == "active" and app.allowed_models is None


def test_hash_de_clave_unico(session):
    org = _org(session)
    app = _app(session, org)
    session.add(_key(org, app))
    session.flush()
    session.add(_key(org, app))
    with pytest.raises(IntegrityError):
        session.flush()


def test_clave_de_aplicacion_no_puede_tener_persona(session):
    org = _org(session)
    app = _app(session, org)
    user = User(organization_id=org.id, email="ana@example.com", name="Ana")
    session.add(user)
    session.flush()
    session.add(_key(org, app, user))
    # MySQL informa del CHECK violado como error 3819 (pymysql lo trae como OperationalError)
    with pytest.raises(DBAPIError, match="ck_api_keys_owner_matches_kind"):
        session.flush()


def test_clave_personal_sin_persona_falla(session):
    org = _org(session)
    session.add(_key(org, kind=KeyKind.USER, raw="plgw-usr-x"))
    # MySQL informa del CHECK violado como error 3819 (pymysql lo trae como OperationalError)
    with pytest.raises(DBAPIError, match="ck_api_keys_owner_matches_kind"):
        session.flush()


def test_slug_de_equipo_unico_por_organizacion(session):
    org = _org(session)
    session.add_all([Team(organization_id=org.id, slug="a", name="A"), Team(organization_id=org.id, slug="a", name="B")])
    with pytest.raises(IntegrityError):
        session.flush()


def test_catalogo_precios_y_auditoria(session):
    from datetime import datetime
    from decimal import Decimal

    provider = Provider(slug="anthropic", name="Anthropic", kind=ProviderKind.ANTHROPIC, api_key_encrypted="enc:v1:x")
    model = LlmModel(model_key="claude-sonnet-5-5", display_name="Claude Sonnet 5.5", family=ModelFamily.ANTHROPIC,
                     capabilities=["tools", "thinking", "structured_output", "prompt_cache"])
    session.add_all([provider, model])
    session.flush()
    session.add(Deployment(model_id=model.id, provider_id=provider.id, upstream_model="claude-sonnet-5-5"))
    session.add(ModelPrice(model_id=model.id, effective_from=datetime(2026, 10, 1), input_per_mtok=Decimal("3"),
                           output_per_mtok=Decimal("15")))
    log = AuditLog(request_id="req-1", contract=Contract.ANTHROPIC, organization_id=1, cost_usd=Decimal("0.00012345"),
                   created_at=datetime(2026, 10, 31, 9, 0, 0, 123456))
    session.add(log)
    session.flush()
    session.refresh(log)
    assert log.status == "pending" and log.cost_usd == Decimal("0.00012345")
    # DATETIME(6): conserva los microsegundos
    assert log.created_at.microsecond == 123456
