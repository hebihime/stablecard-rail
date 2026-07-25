"""Migrations and models must not drift.

The whole suite runs on an Alembic-migrated database, so this test is what turns
that into an assertion: if a model changes without a migration, it fails here.
"""

from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

import app.funding.models
import app.ledger.models  # noqa: F401 -- registers tables on Base.metadata
from app.core.db import Base


def _include_object(
    _obj: Any, name: str | None, type_: str, _reflected: bool, _compare_to: Any
) -> bool:
    # Alembic's own bookkeeping table is not part of the model metadata.
    return not (type_ == "table" and name == "alembic_version")


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(
        connection, opts={"include_object": _include_object, "compare_type": True}
    )
    return list(compare_metadata(context, Base.metadata))


async def test_models_match_the_migrated_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        differences = await connection.run_sync(_diff)
    assert differences == [], f"models and migrations have drifted: {differences}"


async def test_expected_tables_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync_conn: set(Base.metadata.tables) & set(_reflect_table_names(sync_conn))
        )
    assert names == {"funding_intents", "ledger_events"}


def _reflect_table_names(connection: Connection) -> list[str]:
    from sqlalchemy import inspect

    return inspect(connection).get_table_names()
