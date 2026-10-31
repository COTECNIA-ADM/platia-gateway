"""Entorno de Alembic con el motor asíncrono del gateway.

La URL sale de `app.settings` salvo que quien llama fije `sqlalchemy.url` (los tests).
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from app.settings import Settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # conexión síncrona prestada (tests)
        _do_run(connection)
    else:
        asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
