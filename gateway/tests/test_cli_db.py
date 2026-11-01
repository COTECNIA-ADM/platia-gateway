"""Alta de la configuración con `python -m app.cli` y uso de la clave creada."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli
from app.crypto import decrypt, load_key
from app.main import create_app
from app.models import ApiKey, Deployment, LlmModel, ModelPrice, Provider

from .conftest import ANTHROPIC_REPLY, KEY_B64, needs_db

pytestmark = needs_db


@pytest.fixture
def settings(client):  # base limpia y la misma configuración que los tests de la API
    return client.app.state.settings


def _run(settings, capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv), settings=settings)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_alta_completa_y_uso(settings, capsys, monkeypatch, engine):
    monkeypatch.setenv("CLAVE_PROVEEDOR", "sk-ant-cli")
    steps = [
        ("create-org", "--slug", "acme", "--name", "Acme", "--providers", "anthropic-cli"),
        ("create-team", "--org", "acme", "--slug", "ia", "--name", "IA"),
        ("create-app", "--org", "acme", "--team", "ia", "--slug", "bot", "--name", "Bot", "--trusted",
         "--purpose", "resumen", "--model", "claude-cli"),
        ("create-provider", "--slug", "anthropic-cli", "--name", "Anthropic", "--kind", "anthropic",
         "--base-url", "https://anthropic.test", "--api-key-env", "CLAVE_PROVEEDOR"),
        ("create-model", "--key", "claude-cli", "--name", "Claude CLI", "--family", "anthropic",
         "--max-output-tokens", "1000", "--capability", "tools"),
        ("create-deployment", "--model", "claude-cli", "--provider", "anthropic-cli", "--upstream", "claude-upstream"),
        ("set-price", "--model", "claude-cli", "--input", "3", "--output", "15", "--since", "2026-01-01"),
    ]
    for step in steps:
        code, out, err = _run(settings, capsys, *step)
        assert code == 0, err
    code, out, _ = _run(settings, capsys, "create-key", "--org", "acme", "--app", "bot", "--label", "pruebas")
    assert code == 0
    raw = out.strip().splitlines()[-1]
    assert raw.startswith("plgw-app-bot-")

    with Session(engine) as db:
        provider = db.execute(select(Provider).where(Provider.slug == "anthropic-cli")).scalar_one()
        assert "sk-ant-cli" not in provider.api_key_encrypted
        assert decrypt(provider.api_key_encrypted, key=load_key(KEY_B64), context="provider:anthropic-cli") == "sk-ant-cli"
        key = db.execute(select(ApiKey).where(ApiKey.label == "pruebas")).scalar_one()
        assert raw not in (key.prefix, key.key_hash) and raw.startswith(key.prefix)
        assert db.execute(select(ModelPrice)).scalars().all()

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=ANTHROPIC_REPLY)

    with TestClient(create_app(settings, transport=httpx.MockTransport(handler))) as api:
        response = api.post("/v1/messages", headers={"x-api-key": raw, "X-Platia-Purpose": "resumen"},
                               json={"model": "claude-cli", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]})
        assert response.status_code == 200, response.text
        assert seen[0].headers["x-api-key"] == "sk-ant-cli"

        code, out, _ = _run(settings, capsys, "show")
        assert "Aplicación bot" in out and "claude-cli → anthropic-cli" in out
        code, out, _ = _run(settings, capsys, "revoke-key", "--prefix", key.prefix)
        assert code == 0
        again = api.post("/v1/messages", headers={"x-api-key": raw},
                            json={"model": "claude-cli", "messages": [{"role": "user", "content": "x"}]})
        assert again.status_code == 401


@pytest.mark.parametrize("argv, message", [
    (("create-team", "--org", "no-existe", "--slug", "a", "--name", "A"), "No existe organizations"),
    (("create-org", "--slug", "cotecnia", "--name", "Repetida"), "ya existe"),
    (("revoke-key", "--prefix", "plgw-app-nada"), "0 claves activas"),
    (("create-provider", "--slug", "x", "--name", "X", "--kind", "openai_compatible"), "--base-url"),
    (("create-provider", "--slug", "x", "--name", "X", "--kind", "anthropic", "--base-url", "http://x"), "https"),
    (("create-provider", "--slug", "x", "--name", "X", "--kind", "anthropic", "--api-key-env", "NO_DEFINIDA"), "vacía"),
])
def test_errores_de_la_linea_de_comandos(settings, capsys, argv, message):
    code, _, err = _run(settings, capsys, *argv)
    assert code == 1 and message in err


def test_proveedor_sin_clave_interno(settings, capsys):
    code, out, _ = _run(settings, capsys, "create-provider", "--slug", "local", "--name", "Local",
                        "--kind", "openai_compatible", "--base-url", "http://llm:8000/v1", "--allow-http", "--no-api-key")
    assert code == 0 and "sin clave" in out


def test_clave_de_proveedor_por_la_entrada(settings, capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-por-stdin\n"))
    code, _, err = _run(settings, capsys, "create-provider", "--slug", "stdin", "--name", "S", "--kind", "anthropic")
    assert code == 0, err


def test_sin_clave_de_cifrado_no_guarda_secretos(settings, capsys):
    from dataclasses import replace
    code, _, err = _run(replace(settings, encryption_key=""), capsys, "create-provider", "--slug", "y", "--name", "Y",
                        "--kind", "anthropic")
    assert code == 1 and "ENCRYPTION_KEY" in err


def test_precio_no_valido(settings, capsys):
    with pytest.raises(SystemExit):
        cli.main(["set-price", "--model", "claude-test", "--input", "-1", "--output", "abc"], settings=settings)


def test_clave_para_aplicacion_suspendida(settings, capsys, engine):
    from sqlalchemy import text
    with Session(engine) as db:
        db.execute(text("UPDATE applications SET status = 'suspended' WHERE slug = 'otra'"))
        db.commit()
    code, _, err = _run(settings, capsys, "create-key", "--org", "cotecnia", "--app", "otra")
    assert code == 1 and "no está activa" in err


def test_consumo_por_proposito_y_modelo(settings, capsys, client, world):
    headers = {"x-api-key": world.keys["platia"], "X-Platia-Purpose": "proposal_writer",
               "X-Request-ID": "tarea-platia-0001"}
    body = {"model": "claude-test", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}
    for _ in range(2):
        assert client.post("/v1/messages", json=body, headers=headers).status_code == 200
    code, out, _ = _run(settings, capsys, "usage", "--app", "platia", "--since", "2020-01-01",
                        "--until", "2100-01-01", "--request-id", "tarea-platia-0001")
    assert code == 0
    line = next(row for row in out.splitlines() if row.startswith("proposal_writer"))
    # 2 llamadas de 1000 entrada, 500 salida y 2000 de caché: 0,0111 USD cada una
    assert line.split()[1:6] == ["claude-test", "2", "2", "2000", "1000"]
    assert "Total: 0.022200 USD en 2 llamadas" in out
    code, _, err = _run(settings, capsys, "usage", "--app", "no-existe")
    assert code == 1 and "no-existe" in err


def test_repetir_con_exists_ok(settings, capsys):
    code, out, _ = _run(settings, capsys, "--exists-ok", "create-org", "--slug", "cotecnia", "--name", "Otra vez")
    assert code == 0 and "Ya existía" in out


def test_cadena_de_reserva_y_despliegues(settings, capsys, engine, world):
    code, out, _ = _run(settings, capsys, "create-model", "--key", "claude-mini", "--name", "Mini", "--family",
                        "anthropic", "--fallback", "claude-test", "--fallback", "gpt-test", "--fallback", "claude-test")
    assert code == 0
    code, out, _ = _run(settings, capsys, "set-fallbacks", "--model", "gpt-test", "--to", "claude-mini")
    assert (code, out.strip()) == (0, "Reserva de 'gpt-test': claude-mini")
    code, out, err = _run(settings, capsys, "set-fallbacks", "--model", "gpt-test", "--to", "gpt-test")
    assert code == 1 and "propia reserva" in err
    code, out, err = _run(settings, capsys, "set-fallbacks", "--model", "gpt-test", "--to", "no-existe")
    assert code == 1 and "No existe" in err

    with Session(engine) as db:
        deployment = db.execute(select(Deployment).where(Deployment.model_id == world.claude_id)).scalar_one()
    code, out, _ = _run(settings, capsys, "set-deployment", "--id", str(deployment.id), "--priority", "2",
                        "--weight", "0", "--inactive")
    assert out.strip() == f"Despliegue {deployment.id}: prioridad 2, peso 0, inactivo"
    code, out, _ = _run(settings, capsys, "show")
    assert f"[despliegue {deployment.id}, prioridad 2, peso 0, inactivo]" in out
    assert "Reserva de claude-mini: claude-test → gpt-test" in out
    assert "Reserva de gpt-test: claude-mini" in out

    code, out, _ = _run(settings, capsys, "set-fallbacks", "--model", "gpt-test")
    assert out.strip() == "Reserva de 'gpt-test': ninguna"
    with Session(engine) as db:
        assert db.execute(select(LlmModel.fallback_models).filter_by(model_key="gpt-test")).scalar_one() is None
        assert db.execute(select(LlmModel.fallback_models).filter_by(model_key="claude-mini")).scalar_one() == [
            "claude-test", "gpt-test"]
    code, out, _ = _run(settings, capsys, "set-deployment", "--id", str(deployment.id), "--active")
    assert out.strip().endswith("activo") and "inactivo" not in out


def test_limites_por_cli(settings, capsys, client, world):
    def run(*argv):
        return _run(settings, capsys, *argv)

    code, out, _ = run("set-limit", "--org", "cotecnia", "--metric", "cost_usd", "--period", "month", "--value", "50")
    assert code == 0 and "organización cotecnia, cost_usd month = 50" in out
    code, out, _ = run("set-limit", "--org", "cotecnia", "--metric", "cost_usd", "--period", "month", "--value", "80",
                       "--alert-pct", "90")
    assert code == 0 and "actualizado" in out  # mismo ámbito, métrica y periodo: se cambia, no se duplica
    assert run("set-limit", "--org", "cotecnia", "--team", "plataforma", "--metric", "requests", "--period", "minute",
               "--value", "60")[0] == 0
    assert run("set-limit", "--org", "cotecnia", "--app", "platia", "--metric", "concurrent", "--value", "4")[0] == 0
    code, out, _ = run("set-limit", "--org", "cotecnia", "--app", "platia", "--user", "", "--metric", "requests",
                       "--period", "day", "--value", "100")
    assert code == 0 and "cada persona de platia" in out
    code, out, _ = run("set-limit", "--org", "cotecnia", "--app", "platia", "--user", "7", "--metric", "cost_usd",
                       "--period", "day", "--value", "2")
    assert code == 0 and "persona 7 de platia" in out

    assert client.post("/v1/messages", json={"model": "claude-test", "max_tokens": 10,
                                             "messages": [{"role": "user", "content": "Hola"}]},
                       headers={"x-api-key": world.keys["platia"], "X-Platia-User-Id": "7"}).status_code == 200
    capsys.readouterr()  # los logs de la petición
    code, out, _ = run("limits")
    assert code == 0
    lines = out.strip().splitlines()
    assert len(lines) == 5
    assert "organización cotecnia: cost_usd month ≤ 80 (aviso 90 %) · usado 0.011100" in lines[0]
    assert any("equipo plataforma: requests minute ≤ 60" in line and "usado" not in line for line in lines)
    assert any("persona 7 de platia: cost_usd day ≤ 2 (aviso 80 %) · usado 0.011100" in line for line in lines)
    assert any("personas de platia: requests day ≤ 100" in line and "usado" not in line for line in lines)

    limit_id = lines[0].split("]")[0].strip("[")
    assert run("remove-limit", "--id", limit_id)[0] == 0
    assert len(run("limits")[1].strip().splitlines()) == 4
    assert run("remove-limit", "--id", limit_id)[0] == 1  # ya no existe


def test_limites_por_cli_errores(settings, capsys, client):
    def error(*argv):
        code, _, err = _run(settings, capsys, *argv)
        assert code == 1
        return err

    assert "No hay límites" in _run(settings, capsys, "limits")[1]
    assert "Falta --period" in error("set-limit", "--org", "cotecnia", "--metric", "requests", "--value", "1")
    assert "por día o por mes" in error("set-limit", "--org", "cotecnia", "--metric", "cost_usd", "--period", "minute",
                                        "--value", "1")
    assert "--alert-pct" in error("set-limit", "--org", "cotecnia", "--metric", "tokens", "--period", "minute",
                                  "--value", "1", "--alert-pct", "0")
    assert "--user necesita --app" in error("set-limit", "--org", "cotecnia", "--user", "7", "--metric", "requests",
                                            "--period", "day", "--value", "1")
    assert "no los dos" in error("set-limit", "--org", "cotecnia", "--team", "plataforma", "--app", "platia",
                                 "--metric", "requests", "--period", "day", "--value", "1")
    assert "No existe" in error("set-limit", "--org", "nadie", "--metric", "requests", "--period", "day", "--value", "1")


def test_politica_de_contenido_por_cli(settings, capsys, client, world):
    def run(*argv):
        return _run(settings, capsys, *argv)

    code, out, _ = run("content-policies", "--org", "cotecnia")
    assert code == 0 and "input  jailbreak  block  (por defecto)" in out
    assert run("set-content-policy", "--org", "cotecnia", "--direction", "input", "--check", "pii",
               "--action", "redact")[0] == 0
    code, out, _ = run("set-content-policy", "--org", "cotecnia", "--app", "platia", "--direction", "input",
                       "--check", "pii", "--action", "log")
    assert code == 0 and "aplicación platia: input.pii = log" in out
    assert "input  pii        redact (organización)" in run("content-policies", "--org", "cotecnia")[1]
    assert "input  pii        log    (aplicación)" in run("content-policies", "--org", "cotecnia", "--app",
                                                           "platia")[1]
    # Volver a lo heredado y cambiar de nuevo reutiliza la fila
    assert run("set-content-policy", "--org", "cotecnia", "--app", "platia", "--direction", "input",
               "--check", "pii", "--action", "default")[0] == 0
    assert "input  pii        redact (organización)" in run("content-policies", "--org", "cotecnia", "--app",
                                                              "platia")[1]
    assert run("set-content-policy", "--org", "cotecnia", "--app", "platia", "--direction", "input",
               "--check", "pii", "--action", "block")[0] == 0
    assert "input  pii        block  (aplicación)" in run("content-policies", "--org", "cotecnia", "--app",
                                                             "platia")[1]

    code, _, err = run("set-content-policy", "--org", "cotecnia", "--direction", "input", "--check", "jailbreak",
                       "--action", "redact")
    assert code == 1 and "no vale para jailbreak" in err
    code, _, err = run("set-content-policy", "--org", "cotecnia", "--direction", "output", "--check",
                       "classified", "--action", "log")
    assert code == 1 and "En la salida no se comprueba" in err


def test_personas_por_cli(settings, capsys, monkeypatch, client, engine):
    """Alta de una persona, contraseña, clave personal y bajas (G10)."""
    from app.models import User, UserSession

    code, out, _ = _run(settings, capsys, "create-app", "--org", "cotecnia", "--team", "plataforma", "--slug",
                        "herramientas", "--name", "Herramientas", "--personal-keys")
    assert code == 0 and "para claves personales" in out
    code, _, err = _run(settings, capsys, "create-app", "--org", "cotecnia", "--team", "plataforma", "--slug",
                        "otra-mas", "--name", "Otra", "--personal-keys")
    assert code == 1 and "ya tiene una aplicación" in err
    code, _, err = _run(settings, capsys, "create-app", "--org", "cotecnia", "--team", "plataforma", "--slug",
                        "confiada", "--name", "C", "--personal-keys", "--trusted")
    assert code == 1 and "no puede ser de confianza" in err

    code, out, _ = _run(settings, capsys, "create-user", "--org", "cotecnia", "--email", "Eva@Cotecnia.es",
                        "--name", "Eva", "--team", "plataforma", "--role", "admin")
    assert code == 0 and "eva@cotecnia.es" in out and "rol admin" in out
    monkeypatch.setenv("CLAVE_EVA", "corta")
    code, _, err = _run(settings, capsys, "set-password", "--org", "cotecnia", "--email", "eva@cotecnia.es",
                        "--password-env", "CLAVE_EVA")
    assert code == 1 and "12 caracteres" in err
    monkeypatch.setenv("CLAVE_EVA", "Una contraseña para Eva")
    code, out, _ = _run(settings, capsys, "set-password", "--org", "cotecnia", "--email", "eva@cotecnia.es",
                        "--password-env", "CLAVE_EVA")
    assert code == 0 and "sesiones cerradas: 0" in out

    login = client.post("/auth/login", json={"email": "eva@cotecnia.es", "password": "Una contraseña para Eva"})
    assert login.json()["scope"] == "mfa_setup"  # administradora sin MFA

    code, out, _ = _run(settings, capsys, "create-key", "--org", "cotecnia", "--user", "eva@cotecnia.es",
                        "--label", "scripts", "--expires-days", "10")
    raw = out.strip().splitlines()[-1]
    assert code == 0 and raw.startswith("plgw-usr-") and "caduca en 10 días" in out
    ask = {"model": "claude-test", "max_tokens": 5, "messages": [{"role": "user", "content": "Hola"}]}
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ask).status_code == 200
    code, _, err = _run(settings, capsys, "create-key", "--org", "cotecnia")
    assert code == 1 and "--app" in err

    code, out, _ = _run(settings, capsys, "reset-mfa", "--org", "cotecnia", "--email", "eva@cotecnia.es")
    assert code == 0 and "sesiones cerradas: 1" in out
    code, out, _ = _run(settings, capsys, "revoke-sessions", "--org", "cotecnia", "--email", "eva@cotecnia.es",
                        "--keys")
    assert code == 0 and "claves personales revocadas 1" in out
    assert client.post("/v1/messages", headers={"x-api-key": raw}, json=ask).status_code == 401

    code, out, _ = _run(settings, capsys, "set-user", "--org", "cotecnia", "--email", "eva@cotecnia.es",
                        "--role", "member", "--active", "no")
    assert code == 0 and "rol member, inactiva" in out
    with Session(engine) as db:
        eva = db.execute(select(User).where(User.email == "eva@cotecnia.es")).scalar()
        assert eva.password_hash.startswith("scrypt$") and not eva.is_active
        assert db.execute(select(UserSession.revoked_reason)).scalars().all() == ["mfa_changed"]
    code, _, err = _run(settings, capsys, "set-password", "--org", "cotecnia", "--email", "nadie@cotecnia.es",
                        "--password-env", "CLAVE_EVA")
    assert code == 1 and "No existe" in err


def test_contrasena_por_la_entrada(settings, capsys, monkeypatch, client):
    import io
    _run(settings, capsys, "create-user", "--org", "cotecnia", "--email", "leo@cotecnia.es", "--name", "Leo")
    monkeypatch.setattr("sys.stdin", io.StringIO("Contraseña de Leo por la entrada\n"))
    code, _, _ = _run(settings, capsys, "set-password", "--org", "cotecnia", "--email", "leo@cotecnia.es")
    assert code == 0
    login = client.post("/auth/login", json={"email": "leo@cotecnia.es", "password": "Contraseña de Leo por la entrada"})
    assert login.status_code == 200
