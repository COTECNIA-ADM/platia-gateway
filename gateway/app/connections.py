"""Conexiones a MySQL y Redis, iguales para la API y el proceso de tareas de fondo.

Ninguna conecta al crearse: el arranque no depende de que MySQL o Redis estén listos.
"""
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .settings import Settings


def engine_for(settings: Settings, pool_size: int | None = None) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True, pool_recycle=3600,
                               pool_size=pool_size or settings.db_pool_size, max_overflow=settings.db_max_overflow)


def redis_for(settings: Settings) -> Redis:
    return Redis(host=settings.redis_host, port=settings.redis_port, db=settings.redis_db,
                 password=settings.redis_password or None, socket_timeout=settings.health_timeout_seconds,
                 socket_connect_timeout=settings.health_timeout_seconds)
