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
