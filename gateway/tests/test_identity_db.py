"""Inicio de sesión, MFA, sesiones y claves personales contra MySQL (G10, ADR-014)."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import passwords, totp
from app.main import create_app
from app.models import (ApiKey, Application, AuditLog, KeyKind, Organization, Team, User, UserRole,
                        UserSession)
from app.settings import Settings

from .conftest import JWT_SECRET, KEY_B64, URL, needs_db

pytestmark = needs_db

ANA, LUIS = "ana@cotecnia.es", "luis@cotecnia.es"
PASSWORD = "Una contraseña muy larga"
XRW = {"X-Requested-With": "test"}
ASK = {"model": "claude-test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hola"}]}


@pytest.fixture
def people(engine, world):
    with Session(engine) as db:
        team = db.execute(select(Team)).scalar()
        hashed = passwords.hash_password(PASSWORD)
        ana = User(organization_id=world.org_id, team_id=team.id, email=ANA, name="Ana", password_hash=hashed)
        luis = User(organization_id=world.org_id, email=LUIS, name="Luis", role=UserRole.ADMIN, password_hash=hashed)
        tools = Application(organization_id=world.org_id, team_id=team.id, slug="herramientas", name="Herramientas",
                            personal_keys=True, allowed_models=["claude-test"])
        db.add_all([ana, luis, tools])
        db.commit()
        return type("People", (), {"ana": ana.id, "luis": luis.id, "tools": tools.id, "team": team.id})


def _login(client, email=ANA, password=PASSWORD, **extra):
    return client.post("/auth/login", json={"email": email, "password": password, **extra})


def _token(client, email=ANA) -> str:
    response = _login(client, email)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _enable_mfa(client, token) -> tuple[str, str, int]:
    secret = client.post("/auth/mfa/setup", headers=_auth(token)).json()["secret"]
    step = totp.current_step()
    response = client.post("/auth/mfa/enable", headers=_auth(token), json={"code": totp.code_at(secret, step)})
    assert response.status_code == 200, response.text
    return secret, response.json()["access_token"], step


# --- Inicio de sesión

def test_login_sin_mfa_y_me(client, people, world):
    response = _login(client, "  ANA@cotecnia.es ")
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900 and body["scope"] == "full"
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert "platia_gw_refresh=" in cookie and "HttpOnly" in cookie and "Path=/auth" in cookie
    assert "samesite=strict" in cookie.lower() and "Secure" not in cookie  # en desarrollo, sin HTTPS
    me = client.get("/auth/me", headers=_auth(body["access_token"])).json()
    assert me == {"id": people.ana, "email": ANA, "name": "Ana", "role": "member", "organization_id": world.org_id,
                  "mfa_enabled": False, "scope": "full", "platform_operator": False}


def test_credenciales_incorrectas_mismo_mensaje(client, people):
    wrong = _login(client, password="otra contraseña larga")
    unknown = _login(client, email="nadie@cotecnia.es")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"] == "Correo o contraseña incorrectos"
    assert wrong.headers["www-authenticate"] == "Bearer"


def test_bloqueo_tras_varios_fallos(client, people):
    for _ in range(5):
        assert _login(client, password="no es la buena").status_code == 401
    blocked = _login(client)  # ni siquiera con la buena
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    # Otra persona sigue pudiendo entrar
    assert _login(client, LUIS).status_code == 200


def test_sin_redis_no_se_puede_entrar(people, world, engine):
    class Broken:
        async def get(self, *args):
            raise ConnectionError("redis caído")

        async def aclose(self):
            pass

    with TestClient(create_app(_settings(), transport=httpx.MockTransport(lambda r: None),
                               redis_client=Broken())) as client:
        assert _login(client).status_code == 503


def test_sin_jwt_secret_no_hay_inicio_de_sesion(people):
    with TestClient(create_app(_settings(jwt_secret=""), redis_client=_fake_redis())) as client:
        response = _login(client)
        assert response.status_code == 503
        assert "JWT_SECRET" in response.json()["error"]["message"]


def test_cuerpo_no_valido(client, people):
    response = client.post("/auth/login", json={"email": ANA, "password": PASSWORD, "rol": "admin"})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Petición no válida: revisa rol"
    assert client.post("/auth/login", content=b"no es json",
                       headers={"content-type": "application/json"}).status_code == 400


def test_misma_persona_en_dos_organizaciones(client, people, engine):
    with Session(engine) as db:
        other = Organization(slug="otra", name="Otra")
        db.add(other)
        db.flush()
        db.add(User(organization_id=other.id, email=ANA, name="Ana", password_hash=passwords.hash_password(PASSWORD)))
        db.commit()
    response = _login(client)
    assert response.status_code == 400 and "varias organizaciones" in response.json()["error"]["message"]
    assert _login(client, organization="otra").status_code == 200


def test_persona_inactiva_no_entra_y_pierde_la_sesion(client, people, engine):
    token = _token(client)
    with Session(engine) as db:
        db.execute(update(User).where(User.id == people.ana).values(is_active=False))
        db.commit()
    assert client.get("/auth/me", headers=_auth(token)).status_code == 401
    assert _login(client).status_code == 401


def test_token_falso_o_de_otro_tipo(client, people):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=_auth("no.es.un.jwt")).status_code == 401
    # Un vale de MFA no sirve como token de acceso
    token = _token(client, LUIS)
    _enable_mfa(client, token)
    ticket = _login(client, LUIS).json()["mfa_ticket"]
    assert client.get("/auth/me", headers=_auth(ticket)).status_code == 401


# --- MFA

def test_administrador_sin_mfa_solo_puede_configurarlo(client, people, engine):
    response = _login(client, LUIS)
    token = response.json()["access_token"]
    assert response.json()["scope"] == "mfa_setup"
    denied = client.get("/me/keys", headers=_auth(token))
    assert denied.status_code == 403 and "MFA" in denied.json()["error"]["message"]
    assert client.get("/auth/me", headers=_auth(token)).json()["scope"] == "mfa_setup"
    other_session = _token(client, LUIS)

    setup = client.post("/auth/mfa/setup", headers=_auth(token))
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    with Session(engine) as db:
        stored = db.get(User, people.luis).mfa_secret_encrypted
        assert stored.startswith("enc:v1:") and secret not in stored
    bad = client.post("/auth/mfa/enable", headers=_auth(token), json={"code": "000000"})
    assert bad.status_code == 400
    step = totp.current_step()
    enabled = client.post("/auth/mfa/enable", headers=_auth(token), json={"code": totp.code_at(secret, step)})
    assert enabled.json()["scope"] == "full"
    full = enabled.json()["access_token"]
    assert client.get("/me/keys", headers=_auth(full)).status_code == 200
    # Las demás sesiones se cierran al activar el MFA
    assert client.get("/auth/me", headers=_auth(other_session)).status_code == 401
    assert client.post("/auth/mfa/setup", headers=_auth(full)).status_code == 400  # ya activado

    # Inicio de sesión con MFA: primero la contraseña, después el código
    first = _login(client, LUIS).json()
    assert first["mfa_required"] and "access_token" not in first
    wrong = client.post("/auth/login/mfa", json={"mfa_ticket": first["mfa_ticket"], "code": "000000"})
    assert wrong.status_code == 401
    # El código con el que se activó ya está usado: hace falta el siguiente
    reused = client.post("/auth/login/mfa", json={"mfa_ticket": first["mfa_ticket"],
                                                 "code": totp.code_at(secret, step)})
    assert reused.status_code == 401
    ok = client.post("/auth/login/mfa", json={"mfa_ticket": first["mfa_ticket"], "code": totp.code_at(secret, step + 1)})
    assert ok.status_code == 200 and ok.json()["scope"] == "full"
    # El vale no sirve dos veces
    again = client.post("/auth/login/mfa", json={"mfa_ticket": first["mfa_ticket"],
                                                "code": totp.code_at(secret, step + 1)})
    assert again.status_code == 401
    # Un administrador no puede quitárselo
    disable = client.post("/auth/mfa/disable", headers=_auth(full),
                          json={"password": PASSWORD, "code": totp.code_at(secret, step + 1)})
    assert disable.status_code == 403


def test_vale_de_mfa_con_demasiados_intentos(client, people):
    token = _token(client, LUIS)
    secret, _, step = _enable_mfa(client, token)
    ticket = _login(client, LUIS).json()["mfa_ticket"]
    for _ in range(5):
        assert client.post("/auth/login/mfa", json={"mfa_ticket": ticket, "code": "000000"}).status_code == 401
    good = client.post("/auth/login/mfa", json={"mfa_ticket": ticket, "code": totp.code_at(secret, step + 1)})
    assert good.status_code == 401 and "Demasiados intentos" in good.json()["error"]["message"]


def test_persona_activa_y_desactiva_su_mfa(client, people):
    token = _token(client)
    secret, full, step = _enable_mfa(client, token)
    assert client.get("/auth/me", headers=_auth(full)).json()["mfa_enabled"] is True
    bad = client.post("/auth/mfa/disable", headers=_auth(full), json={"password": "no es la buena", "code": "000000"})
    assert bad.status_code == 403
    ok = client.post("/auth/mfa/disable", headers=_auth(full),
                     json={"password": PASSWORD, "code": totp.code_at(secret, step + 1)})
    assert ok.status_code == 204
    assert client.post("/auth/mfa/disable", headers=_auth(full),
                       json={"password": PASSWORD, "code": "123456"}).status_code == 400
    assert "access_token" in _login(client).json()


def test_mfa_sin_clave_de_cifrado(people):
    with TestClient(create_app(_settings(encryption_key=""), redis_client=_fake_redis())) as client:
        token = _token(client)
        assert client.post("/auth/mfa/setup", headers=_auth(token)).status_code == 503


# --- Renovación y cierre

def test_renovacion_rota_y_detecta_copias(client, people):
    first = _login(client)
    old_cookie = first.cookies["platia_gw_refresh"]
    assert client.post("/auth/refresh").status_code == 400  # sin X-Requested-With
    renewed = client.post("/auth/refresh", headers=XRW)
    assert renewed.status_code == 200
    new_cookie = renewed.cookies["platia_gw_refresh"]
    assert new_cookie != old_cookie
    assert client.get("/auth/me", headers=_auth(renewed.json()["access_token"])).status_code == 200
    # Alguien usa la cookie antigua: la sesión entera se cierra
    client.cookies.clear()
    client.cookies.set("platia_gw_refresh", old_cookie, path="/auth")
    stolen = client.post("/auth/refresh", headers=XRW)
    assert stolen.status_code == 401
    assert 'platia_gw_refresh=""' in stolen.headers["set-cookie"] or "Max-Age=0" in stolen.headers["set-cookie"]
    client.cookies.clear()
    client.cookies.set("platia_gw_refresh", new_cookie, path="/auth")
    assert client.post("/auth/refresh", headers=XRW).status_code == 401
    assert client.get("/auth/me", headers=_auth(renewed.json()["access_token"])).status_code == 401


@pytest.mark.parametrize("cookie", ["", "abc", "999.secreto", "1."])
def test_renovacion_con_cookie_rara(client, people, cookie):
    client.cookies.set("platia_gw_refresh", cookie, path="/auth")
    assert client.post("/auth/refresh", headers=XRW).status_code == 401


def test_sesion_caducada(client, people, engine):
    token = _token(client)
    with Session(engine) as db:
        db.execute(text("UPDATE user_sessions SET expires_at = '2020-01-01'"))
        db.commit()
    assert client.get("/auth/me", headers=_auth(token)).status_code == 401
    assert client.post("/auth/refresh", headers=XRW).status_code == 401


def test_logout_con_token_o_con_cookie(client, people, engine):
    token = _token(client)
    assert client.post("/auth/logout", headers=_auth(token)).status_code == 400  # sin X-Requested-With
    response = client.post("/auth/logout", headers={**_auth(token), **XRW})
    assert response.status_code == 204
    assert client.get("/auth/me", headers=_auth(token)).status_code == 401
    # Solo con la cookie (el token ya caducó en el navegador)
    _login(client)
    client.post("/auth/logout", headers={**_auth("caducado"), **XRW})
    with Session(engine) as db:
        reasons = db.execute(select(UserSession.revoked_reason)).scalars().all()
    assert reasons == ["logout", "logout"]
    # Sin nada que cerrar también responde 204
    client.cookies.clear()
    assert client.post("/auth/logout", headers=XRW).status_code == 204
    client.cookies.set("platia_gw_refresh", "1.no-es-el-secreto", path="/auth")
    assert client.post("/auth/logout", headers=XRW).status_code == 204
    client.cookies.set("platia_gw_refresh", "basura", path="/auth")
    assert client.post("/auth/logout", headers=XRW).status_code == 204


def test_cambiar_contrasena_cierra_las_demas_sesiones(client, people):
    other = _token(client)
    token = _token(client)
    assert client.post("/auth/password", headers=_auth(token),
                       json={"current_password": "no es la buena", "new_password": "Otra contraseña larga"}
                       ).status_code == 403
    weak = client.post("/auth/password", headers=_auth(token), json={"current_password": PASSWORD,
                                                                      "new_password": "corta"})
    assert weak.status_code == 400 and "12 caracteres" in weak.json()["error"]["message"]
    ok = client.post("/auth/password", headers=_auth(token),
                     json={"current_password": PASSWORD, "new_password": "Otra contraseña larga"})
    assert ok.json() == {"other_sessions_closed": 1}
    assert client.get("/auth/me", headers=_auth(other)).status_code == 401
    assert client.get("/auth/me", headers=_auth(token)).status_code == 200
    assert _login(client).status_code == 401
    assert _login(client, password="Otra contraseña larga").status_code == 200


def test_listar_y_cerrar_sesiones(client, people):
    other = _token(client)
    token = _token(client)
    data = client.get("/auth/sessions", headers=_auth(token)).json()["data"]
    assert [s["current"] for s in data].count(True) == 1 and len(data) == 2
    assert data[0]["user_agent"] == "testclient" and data[0]["created_at"].endswith("Z")
    other_id = next(s["id"] for s in data if not s["current"])
    assert client.delete(f"/auth/sessions/{other_id}", headers=_auth(token)).status_code == 204
    assert client.get("/auth/me", headers=_auth(other)).status_code == 401
    assert client.delete(f"/auth/sessions/{other_id}", headers=_auth(token)).status_code == 400
    # No se pueden cerrar sesiones de otra persona
    luis_token = _token(client, LUIS)
    _, luis_full, _ = _enable_mfa(client, luis_token)
    luis_session = client.get("/auth/sessions", headers=_auth(luis_full)).json()["data"][0]["id"]
    assert client.delete(f"/auth/sessions/{luis_session}", headers=_auth(token)).status_code == 400
    _token(client)
    _token(client)
    assert client.post("/auth/sessions/revoke-others", headers=_auth(token)).json() == {"sessions_closed": 2}


# --- Claves personales

def test_clave_personal_de_principio_a_fin(client, people, engine, upstream):
    token = _token(client)
    created = client.post("/me/keys", headers=_auth(token), json={"label": "Claude Code", "expires_days": 30})
    assert created.status_code == 201
    body = created.json()
    raw = body["key"]
    assert raw.startswith("plgw-usr-") and body["prefix"] == raw[:17] and body["label"] == "Claude Code"
    listed = client.get("/me/keys", headers=_auth(token)).json()["data"]
    assert [k["id"] for k in listed] == [body["id"]] and "key" not in listed[0]

    # La clave sirve para la inferencia, con las reglas de la aplicación de claves personales
    response = client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK)
    assert response.status_code == 200, response.text
    with Session(engine) as db:
        row = db.execute(select(AuditLog)).scalar()
        assert (row.user_id, row.application_id, row.end_user_ref, row.team_id) == (
            people.ana, people.tools, f"usr:{people.ana}", people.team)
        assert row.api_key_prefix == body["prefix"]
    assert upstream.requests[-1].headers["x-api-key"] == "sk-ant-real"  # nunca la clave personal
    # Modelo fuera de la lista de la aplicación de claves personales
    other = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {raw}"},
                        json={**ASK, "model": "gpt-test"})
    assert other.status_code == 403
    # No puede hacerse pasar por otra persona
    spoof = client.post("/v1/messages", headers={"x-api-key": raw, "X-Platia-User-Id": "7"}, json=ASK)
    assert spoof.status_code == 400
    models = client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).json()["data"]
    assert [m["id"] for m in models] == ["claude-test"]

    assert client.delete(f"/me/keys/{body['id']}", headers=_auth(token)).status_code == 204
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK).status_code == 401
    assert client.delete(f"/me/keys/{body['id']}", headers=_auth(token)).status_code == 400


def test_clave_personal_limites(client, people, engine):
    token = _token(client)
    for _ in range(5):
        assert client.post("/me/keys", headers=_auth(token), json={}).status_code == 201
    too_many = client.post("/me/keys", headers=_auth(token), json={})
    assert too_many.status_code == 400 and "Como mucho 5" in too_many.json()["error"]["message"]
    assert client.post("/me/keys", headers=_auth(token), json={"expires_days": 400}).status_code == 400
    # Cada uno solo ve y revoca sus claves
    luis = _enable_mfa(client, _token(client, LUIS))[1]
    assert client.get("/me/keys", headers=_auth(luis)).json() == {"data": []}
    ana_key = client.get("/me/keys", headers=_auth(token)).json()["data"][0]["id"]
    assert client.delete(f"/me/keys/{ana_key}", headers=_auth(luis)).status_code == 400


def test_clave_personal_sin_aplicacion_para_ellas(client, people, engine):
    token = _token(client)
    raw = client.post("/me/keys", headers=_auth(token), json={}).json()["key"]
    with Session(engine) as db:
        db.execute(update(Application).where(Application.id == people.tools).values(personal_keys=False))
        db.commit()
    blocked = client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK)
    assert blocked.status_code == 403
    assert "claves personales" in blocked.json()["error"]["message"]
    assert client.post("/me/keys", headers=_auth(token), json={}).status_code == 403


def test_clave_personal_de_persona_dada_de_baja(client, people, engine):
    token = _token(client)
    raw = client.post("/me/keys", headers=_auth(token), json={}).json()["key"]
    with Session(engine) as db:
        db.execute(update(User).where(User.id == people.ana).values(is_active=False))
        db.commit()
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ASK).status_code == 401
    with Session(engine) as db:
        key = db.execute(select(ApiKey).where(ApiKey.kind == KeyKind.USER)).scalar()
        assert key.user_id == people.ana and key.application_id is None


# --- Utilidades

def _fake_redis():
    import fakeredis
    return fakeredis.FakeAsyncRedis()


def _settings(**overrides) -> Settings:
    url = make_url(URL)
    values = dict(db_host=url.host, db_port=url.port or 3306, db_user=url.username, db_password=url.password,
                  db_name=url.database, encryption_key=KEY_B64, log_format="console", audit_seal_interval_seconds=0,
                  jwt_secret=JWT_SECRET)
    return Settings(**(values | overrides))
