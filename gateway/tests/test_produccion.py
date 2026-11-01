"""Despliegue (G15, ADR-019): comprobaciones de arranque en producción y proceso de tareas de fondo."""
import asyncio
import base64
import os
import time

import httpx
import pytest

from app import worker
from app.main import create_app
from app.settings import Settings

GOOD = dict(env="production", encryption_key=base64.urlsafe_b64encode(b"k" * 32).decode(), jwt_secret="j" * 40,
            db_password="p" * 20, redis_password="r" * 20)


def test_desarrollo_no_exige_nada():
    assert Settings(env="development", jwt_secret="", encryption_key="").production_problems() == []


def test_produccion_bien_configurada():
    assert Settings(**GOOD).production_problems() == []


@pytest.mark.parametrize("change, variable", [
    ({"encryption_key": ""}, "ENCRYPTION_KEY"),
    ({"jwt_secret": "corta"}, "JWT_SECRET"),
    ({"jwt_secret": "change-me-" + "x" * 40}, "JWT_SECRET"),
    ({"metrics_token": "corto"}, "METRICS_TOKEN"),
    ({"db_password": ""}, "DB_PASSWORD"),
    ({"db_user": "root"}, "DB_USER"),
    ({"redis_password": ""}, "REDIS_PASSWORD"),
    ({"auth_cookie_secure": "false"}, "AUTH_COOKIE_SECURE"),
    ({"require_mfa_for_admins": False}, "REQUIRE_MFA_FOR_ADMINS"),
    ({"log_format": "console"}, "LOG_FORMAT"),
])
def test_produccion_rechaza_lo_inseguro(change, variable):
    settings = Settings(**{**GOOD, **change})
    problems = settings.production_problems()
    assert len(problems) == 1 and problems[0].startswith(variable)
    with pytest.raises(RuntimeError) as error:
        create_app(settings)
    # Nombra la variable, nunca el valor de un secreto
    assert variable in str(error.value)
    for name, value in change.items():
        if name in ("jwt_secret", "metrics_token") and value:
            assert value not in str(error.value)


def test_latido_del_proceso(tmp_path):
    beat = tmp_path / "latido"
    assert not worker.healthy(str(beat))
    beat.write_text("1")
    assert worker.healthy(str(beat))
    assert not worker.healthy(str(beat), now=time.time() + worker.HEARTBEAT_MAX_AGE + 1)


def test_check_por_linea_de_ordenes(tmp_path, monkeypatch):
    beat = tmp_path / "latido"
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(worker_heartbeat_file=str(beat)))
    assert worker.main(["--check"]) == 1
    beat.write_text("1")
    assert worker.main(["--check"]) == 0


def test_main_no_arranca_con_configuracion_insegura(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(**{**GOOD, "jwt_secret": ""}))
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        worker.main([])


async def test_el_proceso_arranca_late_y_para(tmp_path):
    beat = tmp_path / "latido"
    # Sin MySQL ni Redis de verdad: las tareas fallan, lo registran y siguen; el proceso no cae
    settings = Settings(db_host="127.0.0.1", db_port=1, redis_host="127.0.0.1", redis_port=1,
                        worker_heartbeat_file=str(beat), audit_seal_interval_seconds=0.05,
                        webhook_poll_seconds=0.05, health_timeout_seconds=0.2)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(settings, stop, webhook_transport=httpx.MockTransport(lambda r: None)))
    for _ in range(100):
        if beat.exists():
            break
        await asyncio.sleep(0.02)
    assert worker.healthy(str(beat))
    await asyncio.sleep(0.2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 5)
    os.remove(beat)


def test_la_api_no_corre_tareas_de_fondo_si_van_aparte(monkeypatch):
    started = []
    monkeypatch.setattr("app.background.start", lambda deps, http: started.append(1) or [])
    from fastapi.testclient import TestClient
    with TestClient(create_app(Settings(background_tasks=False), checks={})):
        pass
    assert started == []
    with TestClient(create_app(Settings(background_tasks=True), checks={})):
        pass
    assert started == [1]
