"""Configuración del gateway, leída de variables de entorno (plantilla en `.env.example`).

Ningún secreto vive en el código: contraseñas y claves llegan siempre por entorno.
"""
import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: str) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # development | production. En producción no se publica la documentación OpenAPI.
    env: str = field(default_factory=lambda: _env("GATEWAY_ENV", "development"))

    # --- MySQL (base de datos propia del gateway)
    db_host: str = field(default_factory=lambda: _env("DB_HOST", "mysql"))
    db_port: int = field(default_factory=lambda: _int("DB_PORT", "3306"))
    db_user: str = field(default_factory=lambda: _env("DB_USER", "gateway"))
    db_password: str = field(default_factory=lambda: _env("DB_PASSWORD"))
    db_name: str = field(default_factory=lambda: _env("DB_NAME", "platia_gateway"))
    db_pool_size: int = field(default_factory=lambda: _int("DB_POOL_SIZE", "10"))
    db_max_overflow: int = field(default_factory=lambda: _int("DB_MAX_OVERFLOW", "20"))

    # --- Redis (cuotas, rate limiting y caché en fases posteriores)
    redis_host: str = field(default_factory=lambda: _env("REDIS_HOST", "redis"))
    redis_port: int = field(default_factory=lambda: _int("REDIS_PORT", "6379"))
    redis_password: str = field(default_factory=lambda: _env("REDIS_PASSWORD"))
    redis_db: int = field(default_factory=lambda: _int("REDIS_DB", "0"))

    # --- Cifrado en reposo de secretos de proveedor (AES-256-GCM): 32 bytes en base64 url-safe
    encryption_key: str = field(default_factory=lambda: _env("ENCRYPTION_KEY"))

    # --- Llamadas a los proveedores (G3). La lectura es larga: una respuesta sin streaming
    # de un modelo con razonamiento puede tardar minutos.
    provider_connect_timeout_seconds: float = field(
        default_factory=lambda: _float("PROVIDER_CONNECT_TIMEOUT_SECONDS", "10"))
    provider_timeout_seconds: float = field(default_factory=lambda: _float("PROVIDER_TIMEOUT_SECONDS", "300"))
    # Tamaño máximo del cuerpo de una petición (Nginx tiene su propio límite, igual o mayor)
    max_body_bytes: int = field(default_factory=lambda: _int("MAX_BODY_BYTES", str(10 * 1024 * 1024)))

    # --- Comprobaciones de salud: tiempo máximo por dependencia
    health_timeout_seconds: float = field(default_factory=lambda: _float("HEALTH_TIMEOUT_SECONDS", "2"))

    # --- Logs
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_format: str = field(default_factory=lambda: _env("LOG_FORMAT", "json"))

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def database_url(self) -> str:
        # La contraseña puede llevar caracteres reservados de URL: se escapa
        user, password = quote(self.db_user, safe=""), quote(self.db_password, safe="")
        return (f"mysql+aiomysql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
                "?charset=utf8mb4")

    @property
    def sync_database_url(self) -> str:
        """La misma base con el driver síncrono (PyMySQL), para la línea de comandos."""
        return self.database_url.replace("mysql+aiomysql://", "mysql+pymysql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
