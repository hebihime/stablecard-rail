"""Alembic environment.

The database URL comes from application settings unless a caller (the test suite)
injects one, so migrations and the running service can never drift onto different
databases.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.chain.cursors
import app.funding.models
import app.funding.routes
import app.ledger.models
import app.webhooks.models  # noqa: F401 -- registers tables on Base.metadata
from alembic import context
from app.core.config import get_settings
from app.core.db import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` matters more than it looks. The default is
    # True, which switches off every logger that already exists when this runs — and
    # in the test suite that is all of them, because the session-scoped
    # `migrated_database` fixture runs migrations after `app.*` has been imported.
    # The symptom was that no adapter's fail-closed webhook warning could be observed
    # by any test: `caplog.text` came back empty while the same call logged correctly
    # outside pytest. Found in phase 4 (docs/ARCHITECTURE.md §8.9).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
        **kwargs,
    )


def _include_object(
    _object: Any, name: str | None, type_: str, _reflected: bool, _compare_to: Any
) -> bool:
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
