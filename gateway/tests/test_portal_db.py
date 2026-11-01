"""Portal de usuario contra MySQL (G14, RF-17): solo datos propios."""
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import passwords
from app.models import (Application, LimitMetric, LimitPeriod, LimitScope, Team, UsageLimit, User, UserRole)

from .conftest import needs_db

pytestmark = needs_db

PASSWORD = "Una contraseña muy larga"
ASK = {"model": "claude-test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hola"}]}


@pytest.fixture
def people(engine, world):
    hashed = passwords.hash_password(PASSWORD)
    with Session(engine) as db:
        team = db.execute(select(Team)).scalar()
        ana = User(organization_id=world.org_id, team_id=team.id, email="ana@cotecnia.es", name="Ana",
                   password_hash=hashed)
        luis = User(organization_id=world.org_id, email="luis@cotecnia.es", name="Luis", password_hash=hashed,
                    role=UserRole.MEMBER)
        tools = Application(organization_id=world.org_id, team_id=team.id, slug="herramientas", name="Herramientas",
                            personal_keys=True, allowed_models=["claude-test"])
        db.add_all([ana, luis, tools])
        db.flush()
        db.add_all([
            # De cada persona: sale en el portal con el consumo de quien pregunta
            UsageLimit(scope=LimitScope.END_USER, scope_id=tools.id,
                       metric=LimitMetric.REQUESTS, period=LimitPeriod.DAY, limit_value=Decimal(10)),
            # De otra persona y de toda la organización: no salen
            UsageLimit(scope=LimitScope.END_USER, scope_id=tools.id,
                       subject=f"usr:{luis.id}", metric=LimitMetric.COST_USD, period=LimitPeriod.MONTH,
                       limit_value=Decimal(5)),
            UsageLimit(scope=LimitScope.ORGANIZATION, scope_id=world.org_id,
                       metric=LimitMetric.COST_USD, period=LimitPeriod.MONTH, limit_value=Decimal(1000)),
        ])
        db.commit()
        return type("People", (), {"ana": ana.id, "luis": luis.id, "tools": tools.id})


def _headers(client, email) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _key(client, headers) -> tuple[int, str]:
    created = client.post("/me/keys", headers=headers, json={"label": "portátil", "expires_days": 30})
    assert created.status_code == 201, created.text
    return created.json()["id"], created.json()["key"]


def test_portal_needs_a_session(client, people):
    assert client.get("/me/portal").status_code == 401
    assert client.get("/me/usage", headers={"Authorization": "Bearer nada"}).status_code == 401


def test_portal_shows_own_account_models_and_limits(client, people):
    headers = _headers(client, "ana@cotecnia.es")
    _, raw = _key(client, headers)
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK).status_code == 200
    data = client.get("/me/portal", headers=headers).json()
    assert data["user"]["email"] == "ana@cotecnia.es" and data["user"]["organization"] == "Cotecnia"
    assert data["user"]["team"] == "Plataforma"
    assert data["personal_keys"]["enabled"] is True and data["personal_keys"]["max_keys"] == 5
    assert [m["model"] for m in data["models"]] == ["claude-test"]
    assert data["models"][0]["input_per_mtok"] == "3"
    assert data["limits"] == [{"metric": "requests", "period": "day", "value": "10", "alert_pct": 80, "used": "1",
                               "only_you": False}]


def test_portal_without_personal_keys(client, engine, people):
    with Session(engine) as db:
        db.get(Application, people.tools).personal_keys = False
        db.commit()
    data = client.get("/me/portal", headers=_headers(client, "ana@cotecnia.es")).json()
    assert data["personal_keys"]["enabled"] is False and data["models"] == [] and data["limits"] == []


def test_usage_and_requests_are_only_mine(client, people, world):
    ana = _headers(client, "ana@cotecnia.es")
    luis = _headers(client, "luis@cotecnia.es")
    _, ana_key = _key(client, ana)
    _, luis_key = _key(client, luis)
    for _ in range(3):
        assert client.post("/v1/messages", headers={"x-api-key": ana_key}, json=ASK).status_code == 200
    assert client.post("/v1/messages", headers={"x-api-key": luis_key}, json=ASK).status_code == 200
    # Y las de la aplicación PLATIA, que no son de nadie del portal
    assert client.post("/v1/messages", headers={"x-api-key": world.keys["platia"]}, json=ASK).status_code == 200

    usage = client.get("/me/usage?days=7", headers=ana).json()
    assert usage["totals"]["requests"] == 3 and usage["totals"]["errors"] == 0
    assert Decimal(usage["totals"]["cost_usd"]) > 0
    assert usage["by_model"][0]["model"] == "claude-test" and usage["by_model"][0]["requests"] == 3
    assert sum(d["requests"] for d in usage["series"]) == 3
    assert client.get("/me/usage?days=0", headers=ana).status_code == 400

    first = client.get("/me/requests?limit=2", headers=ana).json()
    assert len(first["data"]) == 2 and first["next"]
    rest = client.get(f"/me/requests?limit=2&before_id={first['next']}", headers=ana).json()
    assert len(rest["data"]) == 1 and rest["next"] is None
    assert {r["application"] for r in first["data"] + rest["data"]} == {"herramientas"}
    assert "Hola" not in str(first)
    assert client.get("/me/usage", headers=luis).json()["totals"]["requests"] == 1


def test_rotate_keeps_the_old_key_for_a_while(client, people):
    headers = _headers(client, "ana@cotecnia.es")
    key_id, old = _key(client, headers)
    rotated = client.post(f"/me/keys/{key_id}/rotate", headers=headers, json={"grace_hours": 24})
    assert rotated.status_code == 201, rotated.text
    assert rotated.headers["cache-control"] == "no-store"
    body = rotated.json()
    assert body["label"] == "portátil" and body["key"] != old and body["previous"]["revoked_at"] is None
    # Las dos valen mientras dura el margen
    assert client.post("/v1/messages", headers={"x-api-key": old}, json=ASK).status_code == 200
    assert client.post("/v1/messages", headers={"x-api-key": body["key"]}, json=ASK).status_code == 200
    # Sin margen, la anterior deja de valer ya
    again = client.post(f"/me/keys/{body['id']}/rotate", headers=headers, json={"grace_hours": 0}).json()
    assert client.post("/v1/messages", headers={"x-api-key": body["key"]}, json=ASK).status_code == 401
    assert client.post("/v1/messages", headers={"x-api-key": again["key"]}, json=ASK).status_code == 200
    assert client.post(f"/me/keys/{key_id}/rotate", headers=headers, json={"grace_hours": 999}).status_code == 400


def test_rotate_only_own_keys(client, people):
    ana = _headers(client, "ana@cotecnia.es")
    luis = _headers(client, "luis@cotecnia.es")
    key_id, _ = _key(client, ana)
    assert client.post(f"/me/keys/{key_id}/rotate", headers=luis, json={}).status_code == 400
    assert client.post(f"/me/keys/{key_id}/rotate", headers=ana, json={"otra": 1}).status_code == 400


def test_rotate_does_not_count_against_the_maximum(client, people):
    headers = _headers(client, "ana@cotecnia.es")
    ids = [_key(client, headers)[0] for _ in range(5)]
    assert client.post("/me/keys", headers=headers, json={"label": "x", "expires_days": 1}).status_code == 400
    assert client.post(f"/me/keys/{ids[0]}/rotate", headers=headers, json={"grace_hours": 0}).status_code == 201
