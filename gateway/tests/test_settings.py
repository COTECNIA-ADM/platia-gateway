import structlog

from app.logs import configure_logging
from app.settings import Settings, get_settings


def test_valores_por_defecto(monkeypatch):
    for name in ("GATEWAY_ENV", "DB_HOST", "DB_NAME", "REDIS_HOST"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.env == "development"
    assert not settings.is_production
    assert settings.db_host == "mysql"
    assert settings.db_name == "platia_gateway"
    assert settings.redis_host == "redis"


def test_lee_el_entorno(monkeypatch):
    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.setenv("DB_PORT", "3307")
    monkeypatch.setenv("HEALTH_TIMEOUT_SECONDS", "0.5")
    settings = Settings()
    assert settings.is_production
    assert settings.db_port == 3307
    assert settings.health_timeout_seconds == 0.5


def test_database_url_escapa_la_contrasena():
    settings = Settings(db_user="gw", db_password="a/b@c:d?e", db_host="db", db_port=3306, db_name="x")
    assert settings.database_url == "mysql+aiomysql://gw:a%2Fb%40c%3Ad%3Fe@db:3306/x?charset=utf8mb4"


def test_get_settings_se_cachea():
    assert get_settings() is get_settings()


def test_logs_en_json_y_en_consola(capsys):
    configure_logging("INFO", "json")
    structlog.get_logger("t").info("evento.json", clave=1)
    assert '"event": "evento.json"' in capsys.readouterr().out
    configure_logging("DEBUG", "console")
    structlog.get_logger("t").debug("evento.consola")
    assert "evento.consola" in capsys.readouterr().out
