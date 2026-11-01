"""Auditoría verificable (G9): huellas, cadena, sellado, conciliación, disparadores, exportación y RGPD."""
import asyncio
import dataclasses
import importlib.util
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app import audit_chain, cli
from app.background import seal_forever
from app.models import AuditChainState, AuditLog, Organization
from app.models.base import utcnow

from .conftest import GATEWAY_DIR, needs_db

pytestmark = needs_db


@pytest.fixture
def settings(client):
    return client.app.state.settings


def _post(client, world, text="Hola", user=None):
    headers = {"x-api-key": world.keys["platia"]}
    if user:
        headers["X-Platia-User-Id"] = user
    return client.post("/v1/messages", headers=headers, json={
        "model": "claude-test", "max_tokens": 10, "messages": [{"role": "user", "content": text}]})


def _cli(settings, capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv), settings=settings)
    out = capsys.readouterr()
    return code, out.out, out.err


def _rows(engine) -> list[AuditLog]:
    with Session(engine) as db:
        return list(db.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def _async_run(settings, work):
    return cli._run_async(settings, work)


def test_huellas_con_la_sal_de_la_organizacion(client, world, engine):
    _post(client, world, "Resume el pliego", user="7")
    _post(client, world, "Resume el pliego", user="7")
    first, second = _rows(engine)
    with Session(engine) as db:
        salt = db.get(Organization, world.org_id).audit_salt
    assert first.prompt_hash == second.prompt_hash and len(first.prompt_hash) == 64
    assert first.end_user_digest == audit_chain.keyed_digest(salt, "7")
    assert first.prompt_hash != audit_chain.prompt_digest("otra-sal", {"model": "claude-test"})
    assert first.hash_version == "v1" and first.chain_hash is None


def test_sellar_y_verificar(client, world, engine, settings, capsys):
    for _ in range(3):
        _post(client, world)
    code, out, _ = _cli(settings, capsys, "audit-seal")
    assert code == 0 and "Filas selladas: 3" in out
    rows = _rows(engine)
    assert all(r.chain_hash for r in rows)
    assert rows[0].chain_hash == audit_chain.chain_hash(audit_chain.GENESIS, rows[0])
    assert rows[2].chain_hash == audit_chain.chain_hash(rows[1].chain_hash, rows[2])
    code, out, _ = _cli(settings, capsys, "audit-verify")
    assert code == 0 and "Cadena íntegra: 3 filas" in out
    assert "Filas selladas: 0" in _cli(settings, capsys, "audit-seal")[1]  # nada nuevo


def test_una_fila_sellada_no_se_cambia_ni_se_borra(client, world, engine, settings, capsys):
    _post(client, world, user="7")
    _cli(settings, capsys, "audit-seal")
    with engine.begin() as conn, pytest.raises(OperationalError, match="sellada"):
        conn.execute(text("UPDATE audit_logs SET cost_usd = 0"))
    with engine.begin() as conn, pytest.raises(OperationalError, match="no se borran"):
        conn.execute(text("DELETE FROM audit_logs"))
    with engine.begin() as conn, pytest.raises(OperationalError, match="sellada"):
        conn.execute(text("UPDATE audit_logs SET end_user_ref = 'otro'"))  # solo seudónimos


def _recreate_triggers(engine):
    spec = importlib.util.spec_from_file_location(
        "m0005", GATEWAY_DIR / "migrations" / "versions" / "0005_auditoria_encadenada.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn:
        conn.execute(text(module.NO_UPDATE))
        conn.execute(text(module.NO_DELETE))


def test_la_verificacion_detecta_cambios_y_borrados(client, world, engine, settings, capsys):
    for _ in range(4):
        _post(client, world)
    _cli(settings, capsys, "audit-seal")
    rows = _rows(engine)
    ids = [r.id for r in rows]
    # Alguien con permisos de administrador quita los disparadores y toca la tabla
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER trg_audit_logs_sealed"))
        conn.execute(text("DROP TRIGGER trg_audit_logs_no_delete"))
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE audit_logs SET cost_usd = 0 WHERE id = :i"), {"i": ids[1]})
        code, _, err = _cli(settings, capsys, "audit-verify")
        assert code == 1 and f"fila {ids[1]}" in err
        with engine.begin() as conn:  # se deshace el cambio y se borra una fila del medio
            conn.execute(text("UPDATE audit_logs SET cost_usd = :c WHERE id = :i"),
                         {"c": rows[1].cost_usd, "i": ids[1]})
        assert _cli(settings, capsys, "audit-verify")[0] == 0
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_logs WHERE id = :i"), {"i": ids[2]})
        code, _, err = _cli(settings, capsys, "audit-verify")
        assert code == 1 and f"fila {ids[3]}" in err
        with engine.begin() as conn:  # y si se borra el final, lo dice el último eslabón
            conn.execute(text("DELETE FROM audit_logs WHERE id >= :i"), {"i": ids[2]})
        code, _, err = _cli(settings, capsys, "audit-verify")
        assert code == 1 and "faltan filas del final" in err
    finally:
        _recreate_triggers(engine)


def test_una_fila_sin_sellar_entre_selladas_se_detecta(client, world, engine, settings, capsys):
    insert = text("INSERT INTO audit_logs (id, request_id, created_at, contract, stream, status, input_tokens,"
                  " output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, hash_version, attempts)"
                  " VALUES (:i, 'a-mano', NOW(6), 'anthropic', 0, 'success', 0, 0, 0, 0, 0, 'v1', 1)")
    _post(client, world)
    with engine.begin() as conn:  # un hueco en los ids, como el que deja un insert fallido
        conn.execute(insert, {"i": 10})
    _cli(settings, capsys, "audit-seal")
    with engine.begin() as conn:  # alguien mete una fila en el hueco, por debajo de lo sellado
        conn.execute(insert, {"i": 5})
    code, _, err = _cli(settings, capsys, "audit-verify")
    assert code == 1 and "sin sellar entre filas selladas" in err and "fila 5" in err


def test_una_pendiente_detiene_el_sellado_hasta_la_conciliacion(client, world, engine, settings, capsys):
    _post(client, world)
    with Session(engine) as db:  # una petición que se quedó a medias (el proceso murió)
        db.add(AuditLog(request_id="colgada", contract="anthropic", status="pending",
                        created_at=utcnow() - timedelta(hours=2)))
        db.commit()
    _post(client, world)
    settings_long = dataclasses.replace(settings, audit_pending_max_seconds=86400)
    assert "Filas selladas: 1" in _cli(settings_long, capsys, "audit-seal")[1]  # se para en la pendiente
    settings_short = dataclasses.replace(settings, audit_pending_max_seconds=60)
    assert "Filas selladas: 2" in _cli(settings_short, capsys, "audit-seal")[1]
    hung = [r for r in _rows(engine) if r.request_id == "colgada"][0]
    assert (hung.status, hung.error_type) == ("error", "audit_unclosed")
    assert _cli(settings, capsys, "audit-verify")[0] == 0


def test_un_cierre_tardio_no_pisa_la_conciliacion(client, world, engine):
    from app.pipeline import audit
    from app.pipeline.context import PipelineContext
    from app.models import Contract
    _post(client, world)
    [row] = _rows(engine)
    with Session(engine) as db:
        db.execute(update(AuditLog).values(status="error", error_type="audit_unclosed"))
        db.commit()
    deps = client.app.state.deps
    ctx = PipelineContext(request_id="tarde", contract=Contract.ANTHROPIC, headers={})
    ctx.identity, ctx.audit_id = object(), row.id

    async def close():
        engine_async = create_async_engine(deps.settings.database_url)
        try:
            await audit.close(ctx, dataclasses.replace(deps, sessions=async_sessionmaker(engine_async)))
        finally:
            await engine_async.dispose()
    asyncio.run(close())
    assert _rows(engine)[0].error_type == "audit_unclosed"


def test_seudonimizar_no_rompe_la_cadena_y_exportar(client, world, engine, settings, capsys):
    _post(client, world, user="7")
    _post(client, world, user="8")
    _post(client, world, user="7")
    _cli(settings, capsys, "audit-seal")
    code, out, err = _cli(settings, capsys, "audit-export", "--org", "cotecnia", "--user", "7")
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert code == 0 and len(lines) == 2 and "Exportadas 2 filas" in err
    assert lines[0]["end_user_ref"] == "7" and lines[0]["chain_hash"] and "Hola" not in out

    code, out, _ = _cli(settings, capsys, "audit-pseudonymize", "--org", "cotecnia", "--user", "7")
    assert code == 0 and "en 2 filas" in out
    refs = [r.end_user_ref for r in _rows(engine)]
    assert refs[1] == "8" and refs[0] == refs[2] and refs[0].startswith("seud:")
    assert _cli(settings, capsys, "audit-verify")[0] == 0  # la cadena sigue íntegra
    # Tras seudonimizar, la persona aún puede pedir sus datos (por su huella)
    code, out, _ = _cli(settings, capsys, "audit-export", "--org", "cotecnia", "--user", "7", "--pseudonymize")
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert len(lines) == 2 and "end_user_ref" not in lines[0]
    assert _cli(settings, capsys, "audit-export", "--org", "cotecnia", "--app", "platia", "--since", "2020-01-01",
                "--until", "2999-01-01")[2].startswith("Exportadas 3")


def test_el_gateway_sella_en_segundo_plano(client, world, engine, settings):
    _post(client, world)
    _post(client, world)

    async def run():
        engine_async = create_async_engine(settings.database_url)
        deps = dataclasses.replace(client.app.state.deps, sessions=async_sessionmaker(engine_async),
                                   settings=dataclasses.replace(settings, audit_seal_interval_seconds=0.05))
        task = asyncio.create_task(seal_forever(deps))
        await asyncio.sleep(0.5)
        task.cancel()
        await engine_async.dispose()

    asyncio.run(run())
    assert all(r.chain_hash for r in _rows(engine))
    with Session(engine) as db:
        assert db.get(AuditChainState, 1).last_id == _rows(engine)[-1].id


def test_un_fallo_al_sellar_no_para_el_gateway(client, settings, monkeypatch):
    events = []
    monkeypatch.setattr("app.background.log", type("L", (), {"error": lambda self, e, **k: events.append(e)})())

    def broken():
        raise OSError("sin base")

    async def run():
        deps = dataclasses.replace(client.app.state.deps, sessions=broken,
                                   settings=dataclasses.replace(settings, audit_seal_interval_seconds=0.05))
        task = asyncio.create_task(seal_forever(deps))
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(run())
    assert events and set(events) == {"audit.seal_failed"}
