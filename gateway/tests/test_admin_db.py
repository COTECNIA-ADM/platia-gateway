"""API del panel de administración contra MySQL (G13, ADR-017)."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import passwords, totp
from app.models import (AdminAction, ApiKey, Application, AuditLog, AuditStatus, Contract, Organization,
                        Provider, Team, User, UserRole, UserSession)
from app.models.base import utcnow

from .conftest import ANTHROPIC_REPLY, needs_db

pytestmark = needs_db

PASSWORD = "Una contraseña muy larga"
ADMIN, OPERATOR, MEMBER, OUTSIDER = "eva@cotecnia.es", "op@cotecnia.es", "ana@cotecnia.es", "otro@fuera.es"
ASK = {"model": "claude-test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hola"}]}


@pytest.fixture
def staff(engine, world):
    hashed = passwords.hash_password(PASSWORD)
    with Session(engine) as db:
        other = Organization(slug="fuera", name="Fuera")
        db.add(other)
        db.flush()
        other_team = Team(organization_id=other.id, slug="ajeno", name="Ajeno")
        db.add(other_team)
        db.flush()
        other_app = Application(organization_id=other.id, team_id=other_team.id, slug="ajena", name="Ajena")
        team = db.execute(select(Team).filter_by(organization_id=world.org_id)).scalar()
        people = {
            "admin": User(organization_id=world.org_id, email=ADMIN, name="Eva", role=UserRole.ADMIN,
                          password_hash=hashed),
            "operator": User(organization_id=world.org_id, email=OPERATOR, name="Op", role=UserRole.ADMIN,
                             password_hash=hashed, platform_operator=True),
            "member": User(organization_id=world.org_id, team_id=team.id, email=MEMBER, name="Ana",
                           password_hash=hashed),
            "outsider": User(organization_id=other.id, email=OUTSIDER, name="Otro", role=UserRole.ADMIN,
                             password_hash=hashed),
        }
        db.add_all([other_app, *people.values()])
        db.commit()
        return type("Staff", (), {**{k: v.id for k, v in people.items()}, "team": team.id,
                                  "other_team": other_team.id, "other_app": other_app.id, "other_org": other.id})


_MFA: dict[str, list] = {}


def _headers(client, email) -> dict:
    """Entra como `email`. Los administradores necesitan MFA: se activa la primera vez."""
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    data = response.json()
    if "mfa_ticket" in data:
        secret, step = _MFA[email]
        _MFA[email][1] = step + 1
        data = client.post("/auth/login/mfa", json={"mfa_ticket": data["mfa_ticket"],
                                                    "code": totp.code_at(secret, step + 1)}).json()
    elif data.get("scope") == "mfa_setup":
        auth = {"Authorization": f"Bearer {data['access_token']}"}
        secret = client.post("/auth/mfa/setup", headers=auth).json()["secret"]
        step = totp.current_step() - 1
        _MFA[email] = [secret, step]
        enabled = client.post("/auth/mfa/enable", headers=auth, json={"code": totp.code_at(secret, step)})
        assert enabled.status_code == 200, enabled.text
        data = enabled.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.fixture
def as_admin(client, staff):
    return _headers(client, ADMIN)


@pytest.fixture
def as_operator(client, staff):
    return _headers(client, OPERATOR)


def _actions(engine) -> list[AdminAction]:
    with Session(engine) as db:
        return db.execute(select(AdminAction).order_by(AdminAction.id)).scalars().all()


# --- Acceso

def test_admins_need_mfa_for_the_panel(client, staff):
    token = client.post("/auth/login", json={"email": ADMIN, "password": PASSWORD}).json()["access_token"]
    assert client.get("/admin/users", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_only_admins_reach_the_panel(client, staff):
    assert client.get("/admin/applications").status_code == 401
    assert client.get("/admin/applications", headers={"Authorization": "Bearer nada"}).status_code == 401
    member = _headers(client, MEMBER)
    assert client.get("/admin/applications", headers=member).status_code == 403
    # Una clave de aplicación no abre el panel
    app_key = client.get("/admin/applications", headers={"Authorization": "Bearer plgw-xx"})
    assert app_key.status_code == 401


def test_unknown_fields_are_rejected(client, as_admin, staff):
    response = client.post("/admin/teams", headers=as_admin, json={"slug": "datos", "name": "Datos", "rol": "x"})
    assert response.status_code == 400


# --- Equipos, aplicaciones y claves

def test_teams_and_applications_flow(client, engine, as_admin, staff, world):
    team = client.post("/admin/teams", headers=as_admin, json={"slug": "datos", "name": "Datos"})
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]
    assert client.post("/admin/teams", headers=as_admin, json={"slug": "datos", "name": "Otra"}).status_code == 409
    assert client.patch(f"/admin/teams/{team_id}", headers=as_admin, json={"name": "Datos y BI"}).json()["name"] \
        == "Datos y BI"
    slugs = [t["slug"] for t in client.get("/admin/teams", headers=as_admin).json()["data"]]
    assert "datos" in slugs and "ajeno" not in slugs

    created = client.post("/admin/applications", headers=as_admin, json={
        "team_id": team_id, "slug": "informes", "name": "Informes", "allowed_models": ["claude-test"]})
    assert created.status_code == 201, created.text
    app_id = created.json()["id"]
    # Un modelo que no está en el catálogo no se permite
    bad = client.post("/admin/applications", headers=as_admin, json={
        "team_id": team_id, "slug": "mala", "name": "Mala", "allowed_models": ["no-existe"]})
    assert bad.status_code == 400
    # Un equipo de otra organización no existe
    alien = client.post("/admin/applications", headers=as_admin, json={
        "team_id": staff.other_team, "slug": "ajena2", "name": "X"})
    assert alien.status_code == 404
    assert client.get(f"/admin/applications/{staff.other_app}", headers=as_admin).status_code == 404

    patched = client.patch(f"/admin/applications/{app_id}", headers=as_admin,
                           json={"cache_ttl_seconds": 600, "allowed_purposes": ["resumen"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["cache_ttl_seconds"] == 600

    key = client.post(f"/admin/applications/{app_id}/keys", headers=as_admin, json={"label": "producción"})
    assert key.status_code == 201, key.text
    assert key.headers["cache-control"] == "no-store"
    raw = key.json()["key"]
    assert raw.startswith("plgw-")
    listed = client.get(f"/admin/applications/{app_id}/keys", headers=as_admin).json()["data"]
    assert [k["state"] for k in listed] == ["active"] and "key" not in listed[0]

    # La clave sirve para llamar al gateway...
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK).status_code == 200
    # ...hasta que se revoca
    assert client.delete(f"/admin/keys/{listed[0]['id']}", headers=as_admin).status_code == 204
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK).status_code == 401
    everything = client.get("/admin/keys?state=all", headers=as_admin).json()["data"]
    assert {k["id"]: k["state"] for k in everything}[listed[0]["id"]] == "revoked"
    assert listed[0]["id"] not in [k["id"] for k in client.get("/admin/keys", headers=as_admin).json()["data"]]

    actions = [a.action for a in _actions(engine)]
    assert actions[:2] == ["team.create", "team.update"]
    assert {"application.create", "application.update", "key.create", "key.revoke"} <= set(actions)
    assert all(a.organization_id == world.org_id and a.user_id == staff.admin for a in _actions(engine))
    # La clave en claro nunca queda en el registro
    assert raw not in str([a.changes for a in _actions(engine)])


def test_personal_keys_application_rules(client, as_admin, staff):
    first = client.post("/admin/applications", headers=as_admin, json={
        "team_id": staff.team, "slug": "personal", "name": "Personal", "personal_keys": True})
    assert first.status_code == 201, first.text
    second = client.post("/admin/applications", headers=as_admin, json={
        "team_id": staff.team, "slug": "personal2", "name": "Otra", "personal_keys": True})
    assert second.status_code == 400
    trusted = client.post("/admin/applications", headers=as_admin, json={
        "team_id": staff.team, "slug": "mixta", "name": "Mixta", "personal_keys": True, "trusted": True})
    assert trusted.status_code == 400


# --- Personas

def test_users_flow_and_guards(client, engine, as_admin, staff):
    created = client.post("/admin/users", headers=as_admin, json={"email": "Nuevo@Cotecnia.es", "name": "Nuevo",
                                                                  "password": "corta"})
    assert created.status_code == 400  # política de contraseñas
    created = client.post("/admin/users", headers=as_admin, json={"email": "Nuevo@Cotecnia.es", "name": "Nuevo",
                                                                  "password": PASSWORD, "team_id": staff.team})
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["email"] == "nuevo@cotecnia.es" and user["has_password"] is True
    assert client.post("/admin/users", headers=as_admin,
                       json={"email": "nuevo@cotecnia.es", "name": "Dup"}).status_code == 409
    emails = [u["email"] for u in client.get("/admin/users", headers=as_admin).json()["data"]]
    assert "nuevo@cotecnia.es" in emails and OUTSIDER not in emails

    # Quien entra tiene una sesión; cambiarle la contraseña la cierra
    _headers(client, "nuevo@cotecnia.es")
    reset = client.post(f"/admin/users/{user['id']}/password", headers=as_admin, json={"password": PASSWORD + "!"})
    assert reset.json() == {"sessions_closed": 1}
    assert client.post(f"/admin/users/{user['id']}/reset-mfa", headers=as_admin).status_code == 200
    assert client.post(f"/admin/users/{user['id']}/revoke-sessions", headers=as_admin,
                       json={"keys": True}).json() == {"sessions_closed": 0, "keys_revoked": 0}

    # Uno mismo no se degrada, ni se cambia la contraseña aquí, ni se da de baja
    assert client.patch(f"/admin/users/{staff.admin}", headers=as_admin, json={"role": "member"}).status_code == 400
    assert client.post(f"/admin/users/{staff.admin}/password", headers=as_admin,
                       json={"password": PASSWORD}).status_code == 400
    assert client.delete(f"/admin/users/{staff.admin}", headers=as_admin).status_code == 400
    # Otra organización: no existe
    assert client.patch(f"/admin/users/{staff.outsider}", headers=as_admin, json={"name": "X"}).status_code == 404

    # Degradar a otra administradora cierra sus sesiones
    _headers(client, OPERATOR)
    demoted = client.patch(f"/admin/users/{staff.operator}", headers=as_admin, json={"role": "member"})
    assert demoted.status_code == 200 and demoted.json()["active_sessions"] == 0
    assert client.delete(f"/admin/users/{user['id']}", headers=as_admin).status_code == 204
    with Session(engine) as db:
        gone = db.get(User, user["id"])
        assert gone.deleted_at is not None and gone.is_active is False
    record = [a for a in _actions(engine) if a.action == "user.set_password"][0]
    assert record.changes == {"password": "(cambiado)"}
    assert PASSWORD not in str([a.changes for a in _actions(engine)])


def test_the_last_admin_cannot_be_removed(client, engine, staff):
    with Session(engine) as db:
        db.get(User, staff.operator).role = UserRole.MEMBER
        db.commit()
    headers = _headers(client, ADMIN)
    # Eva es la única administradora: ni ella misma ni nadie puede dejar la organización sin una
    assert client.patch(f"/admin/users/{staff.admin}", headers=headers, json={"is_active": False}).status_code == 400


# --- Límites y políticas de contenido

def test_limits_flow(client, engine, as_admin, staff, world):
    created = client.put("/admin/limits", headers=as_admin, json={
        "scope": "application", "scope_id": world.platia_id, "metric": "requests", "period": "day", "value": 2})
    assert created.status_code == 200, created.text
    limit = created.json()
    # Repetirlo con otro valor actualiza el mismo
    again = client.put("/admin/limits", headers=as_admin, json={
        "scope": "application", "scope_id": world.platia_id, "metric": "requests", "period": "day", "value": 1})
    assert again.json()["id"] == limit["id"] and again.json()["value"] == "1"
    # Una aplicación de otra organización no se puede limitar
    alien = client.put("/admin/limits", headers=as_admin, json={
        "scope": "application", "scope_id": staff.other_app, "metric": "requests", "period": "day", "value": 1})
    assert alien.status_code == 404

    headers = {"x-api-key": world.keys["platia"]}
    assert client.post("/v1/messages", headers=headers, json=ASK).status_code == 200
    assert client.post("/v1/messages", headers=headers, json=ASK).status_code == 402  # cupo del día agotado
    listed = client.get("/admin/limits", headers=as_admin).json()["data"]
    assert listed[0]["used"] == "1" and listed[0]["scope_label"] == "aplicación platia"

    assert client.patch(f"/admin/limits/{limit['id']}", headers=as_admin, json={"value": 5}).json()["value"] == "5"
    assert client.delete(f"/admin/limits/{limit['id']}", headers=as_admin).status_code == 204
    assert client.get("/admin/limits", headers=as_admin).json()["data"] == []


def test_content_policies_flow(client, as_admin, world):
    cells = client.get("/admin/content-policies", headers=as_admin).json()["data"]
    pii = next(c for c in cells if c["direction"] == "input" and c["check"] == "pii")
    assert pii["origin"] == "default"
    assert client.put("/admin/content-policies", headers=as_admin, json={
        "direction": "input", "check": "pii", "action": "block"}).status_code == 200
    assert client.put("/admin/content-policies", headers=as_admin, json={
        "application_id": world.platia_id, "direction": "input", "check": "pii", "action": "log"}).status_code == 200
    cells = client.get(f"/admin/content-policies?application_id={world.platia_id}", headers=as_admin).json()["data"]
    pii = next(c for c in cells if c["direction"] == "input" and c["check"] == "pii")
    assert (pii["organization"], pii["application"], pii["effective"], pii["origin"]) == \
        ("block", "log", "log", "application")
    assert client.put("/admin/content-policies", headers=as_admin, json={
        "application_id": world.platia_id, "direction": "input", "check": "pii", "action": "default"}).status_code \
        == 200
    cells = client.get(f"/admin/content-policies?application_id={world.platia_id}", headers=as_admin).json()["data"]
    pii = next(c for c in cells if c["direction"] == "input" and c["check"] == "pii")
    assert (pii["effective"], pii["origin"]) == ("block", "organization")
    assert client.put("/admin/content-policies", headers=as_admin, json={
        "direction": "input", "check": "pii", "action": "nada"}).status_code == 400


# --- Operaciones y auditoría

def test_overview_and_audit(client, engine, as_admin, staff, world):
    headers = {"x-api-key": world.keys["platia"]}
    for _ in range(3):
        assert client.post("/v1/messages", headers=headers, json=ASK).status_code == 200
    with Session(engine) as db:
        db.add(AuditLog(request_id="ajena-1", organization_id=staff.other_org, application_id=staff.other_app,
                        contract=Contract.ANTHROPIC, status=AuditStatus.SUCCESS, http_status=200,
                        created_at=utcnow()))
        db.commit()

    overview = client.get("/admin/overview?hours=24", headers=as_admin).json()
    assert overview["totals"]["requests"] == 3 and overview["totals"]["success"] == 3
    assert overview["by_model"][0]["model"] == "claude-test"
    assert overview["by_application"][0]["application"] == "platia"
    assert sum(point["requests"] for point in overview["series"]) == 3

    first = client.get("/admin/audit?limit=2", headers=as_admin).json()
    assert len(first["data"]) == 2 and first["next"]
    rest = client.get(f"/admin/audit?limit=2&before_id={first['next']}", headers=as_admin).json()
    assert len(rest["data"]) == 1 and rest["next"] is None
    row = first["data"][0]
    assert row["application"] == "platia" and "ajena-1" not in str(first) + str(rest)
    assert client.get(f"/admin/audit/{row['id']}", headers=as_admin).json()["request_id"] == row["request_id"]
    assert client.get(f"/admin/audit?request_id={row['request_id']}", headers=as_admin).json()["data"][0]["id"] \
        == row["id"]
    assert client.get("/admin/audit?status=raro", headers=as_admin).status_code == 400
    assert client.get("/admin/audit?since=ayer", headers=as_admin).status_code == 400
    with Session(engine) as db:
        alien_id = db.execute(select(AuditLog.id).filter_by(request_id="ajena-1")).scalar()
    assert client.get(f"/admin/audit/{alien_id}", headers=as_admin).status_code == 404

    export = client.get("/admin/audit/export?since=2020-01-01", headers=as_admin)
    assert export.status_code == 200 and export.headers["content-type"].startswith("application/x-ndjson")
    assert len(export.text.strip().splitlines()) == 3
    # El texto del prompt nunca está en la auditoría
    assert "Hola" not in export.text and ANTHROPIC_REPLY["content"][0]["text"] not in export.text

    assert client.get("/admin/audit/chain", headers=as_admin).status_code == 403


def test_actions_log_is_scoped(client, engine, as_admin, as_operator, staff):
    client.post("/admin/teams", headers=as_admin, json={"slug": "datos", "name": "Datos"})
    with Session(engine) as db:
        db.add(AdminAction(organization_id=staff.other_org, user_id=staff.outsider, action="team.create",
                           target_type="team", target_id="1"))
        db.add(AdminAction(organization_id=None, user_id=staff.operator, action="model.create",
                           target_type="model", target_id="1"))
        db.commit()
    mine = client.get("/admin/actions", headers=as_admin).json()["data"]
    assert [a["action"] for a in mine] == ["team.create"]
    operator = client.get("/admin/actions", headers=as_operator).json()["data"]
    assert [a["action"] for a in operator] == ["model.create", "team.create"]
    chain = client.get("/admin/audit/chain", headers=as_operator).json()
    assert set(chain) == {"last_sealed_id", "unsealed_rows", "sealed_at"}


# --- Webhooks

def test_webhooks_flow(client, engine, as_admin, as_operator, staff):
    body = {"name": "Avisos", "url": "https://hooks.example.com/platia", "events": ["quota.alert"]}
    created = client.post("/admin/webhooks", headers=as_admin, json=body)
    assert created.status_code == 201, created.text
    assert created.json()["secret"].startswith("plgw-whsec-") and created.json()["global"] is False
    hook_id = created.json()["id"]
    assert "secret" not in client.get("/admin/webhooks", headers=as_admin).json()["data"][0]
    # Destinos internos y eventos desconocidos no valen
    assert client.post("/admin/webhooks", headers=as_admin,
                       json={**body, "url": "https://127.0.0.1/x"}).status_code == 400
    assert client.post("/admin/webhooks", headers=as_admin,
                       json={**body, "events": ["nada"]}).status_code == 400
    # Los globales son de quien opera el gateway
    assert client.post("/admin/webhooks", headers=as_admin, json={**body, "global": True}).status_code == 403
    glob = client.post("/admin/webhooks", headers=as_operator, json={**body, "global": True})
    assert glob.status_code == 201 and glob.json()["global"] is True
    assert client.patch(f"/admin/webhooks/{glob.json()['id']}", headers=as_admin,
                        json={"name": "X"}).status_code == 404

    rotated = client.post(f"/admin/webhooks/{hook_id}/rotate-secret", headers=as_admin).json()["secret"]
    assert rotated != created.json()["secret"]
    off = client.patch(f"/admin/webhooks/{hook_id}", headers=as_admin, json={"is_active": False})
    assert off.json()["is_active"] is False
    assert client.get(f"/admin/webhooks/{hook_id}/deliveries", headers=as_admin).json() == {"data": []}
    assert client.delete(f"/admin/webhooks/{hook_id}", headers=as_admin).status_code == 204
    assert client.get(f"/admin/webhooks/{hook_id}/deliveries", headers=as_admin).status_code == 404
    secrets = (created.json()["secret"], rotated)
    assert not any(s in str([a.changes for a in _actions(engine)]) for s in secrets)


# --- Catálogo

def test_catalog_read_for_admins_write_for_operators(client, engine, as_admin, as_operator, world):
    catalog = client.get("/admin/catalog", headers=as_admin).json()
    assert catalog["can_edit"] is False
    assert {p["slug"]: p["has_api_key"] for p in catalog["providers"]} == {"anthropic": True, "openai": True}
    assert "sk-ant-real" not in str(catalog)
    claude = next(m for m in catalog["models"] if m["model_key"] == "claude-test")
    assert claude["price"]["input_per_mtok"] == "3"
    assert client.post("/admin/models", headers=as_admin, json={
        "model_key": "nuevo", "display_name": "Nuevo", "family": "anthropic"}).status_code == 403
    assert client.get("/admin/catalog", headers=as_operator).json()["can_edit"] is True

    provider = client.post("/admin/providers", headers=as_operator, json={
        "slug": "local", "name": "Modelo local", "kind": "openai_compatible", "base_url": "http://llm:8000/v1"})
    assert provider.status_code == 400  # http solo si se pide
    provider = client.post("/admin/providers", headers=as_operator, json={
        "slug": "local", "name": "Modelo local", "kind": "openai_compatible", "base_url": "http://llm:8000/v1",
        "allow_http": True, "api_key": "sk-local-secreta"})
    assert provider.status_code == 201, provider.text
    assert provider.json()["has_api_key"] is True
    assert client.post("/admin/providers", headers=as_operator, json={
        "slug": "local", "name": "Otro", "kind": "anthropic"}).status_code == 409
    with Session(engine) as db:
        stored = db.execute(select(Provider).filter_by(slug="local")).scalar()
        assert stored.api_key_encrypted and "sk-local-secreta" not in stored.api_key_encrypted
    rotated = client.patch(f"/admin/providers/{provider.json()['id']}", headers=as_operator,
                           json={"api_key": "sk-local-nueva", "name": "Local"})
    assert rotated.json()["name"] == "Local"

    model = client.post("/admin/models", headers=as_operator, json={
        "model_key": "llama-local", "display_name": "Llama", "family": "openai", "fallback_models": ["gpt-test"]})
    assert model.status_code == 201, model.text
    model_id = model.json()["id"]
    assert client.post("/admin/models", headers=as_operator, json={
        "model_key": "otro", "display_name": "X", "family": "openai", "fallback_models": ["nada"]}).status_code == 400
    assert client.patch(f"/admin/models/{model_id}", headers=as_operator,
                        json={"fallback_models": ["llama-local"]}).status_code == 400
    price = client.post(f"/admin/models/{model_id}/prices", headers=as_operator,
                        json={"input_per_mtok": 0.5, "output_per_mtok": 1, "effective_from": "2024-01-01T00:00:00Z"})
    assert price.status_code == 201, price.text
    assert client.post(f"/admin/models/{model_id}/prices", headers=as_operator,
                       json={"input_per_mtok": 1, "output_per_mtok": 2,
                             "effective_from": "2024-01-01T01:00:00+01:00"}).status_code == 409
    assert len(client.get(f"/admin/models/{model_id}/prices", headers=as_admin).json()["data"]) == 1

    deployment = client.post("/admin/deployments", headers=as_operator, json={
        "model_id": model_id, "provider_id": provider.json()["id"], "upstream_model": "llama-3"})
    assert deployment.status_code == 201, deployment.text
    dep_id = deployment.json()["id"]
    assert client.post("/admin/deployments", headers=as_operator, json={
        "model_id": model_id, "provider_id": provider.json()["id"], "upstream_model": "llama-3"}).status_code == 409
    assert client.patch(f"/admin/deployments/{dep_id}", headers=as_operator,
                        json={"weight": 50, "priority": 1}).json()["weight"] == 50
    assert client.delete(f"/admin/deployments/{dep_id}/cooldown", headers=as_operator).status_code == 204

    actions = [a for a in _actions(engine)]
    assert {a.action for a in actions} >= {"provider.create", "provider.update", "model.create", "price.create",
                                           "deployment.create", "deployment.update", "deployment.clear_cooldown"}
    assert all(a.organization_id is None for a in actions)
    changes = str([a.changes for a in actions])
    assert "sk-local" not in changes and "(cambiado)" in changes


def test_cleared_cooldown_brings_a_deployment_back(client, as_operator, redis_fake, world):
    catalog = client.get("/admin/catalog", headers=as_operator).json()
    dep = next(d for d in catalog["deployments"] if d["model_id"] == world.claude_id)
    import asyncio
    asyncio.run(redis_fake.set(f"gw:cooldown:{dep['id']}", "auth", ex=300))
    assert next(d for d in client.get("/admin/catalog", headers=as_operator).json()["deployments"]
                if d["id"] == dep["id"])["cooled"] is True
    assert client.delete(f"/admin/deployments/{dep['id']}/cooldown", headers=as_operator).status_code == 204
    assert next(d for d in client.get("/admin/catalog", headers=as_operator).json()["deployments"]
                if d["id"] == dep["id"])["cooled"] is False


def test_operator_flag_is_not_granted_from_the_panel(client, as_admin, staff):
    response = client.patch(f"/admin/users/{staff.member}", headers=as_admin, json={"platform_operator": True})
    assert response.status_code == 400


def test_sessions_are_counted(client, engine, as_admin, staff):
    with Session(engine) as db:
        count = db.execute(select(UserSession).filter_by(user_id=staff.admin)).scalars().all()
    users = {u["email"]: u for u in client.get("/admin/users", headers=as_admin).json()["data"]}
    assert users[ADMIN]["active_sessions"] == len(count) == 1
    assert users[OPERATOR]["platform_operator"] is True
    with Session(engine) as db:
        db.execute(text("SELECT 1"))
        assert db.execute(select(ApiKey)).scalars().first() is not None
