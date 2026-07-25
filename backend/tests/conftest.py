"""Test bootstrap.

The suite runs against a real PostgreSQL database (see docs/ARCHITECTURE.md,
"Testing against real Postgres"): the ledger relies on JSONB, identity columns,
partial-unique semantics and an append-only trigger, none of which SQLite models
faithfully. The database is created if missing and migrated with Alembic, so
every run also exercises the migration path.

`DATABASE_URL` is rewritten to the test database *before* application modules are
imported, so app code, Alembic and the tests can never target different databases.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_URL = "postgresql+asyncpg://stablecard:stablecard@localhost:5442/stablecard"


def _resolve_test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(os.getenv("DATABASE_URL", _DEFAULT_URL))
    # str(URL) masks the password; render explicitly or connections fail.
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


# Must happen before `app.*` is imported: app settings read DATABASE_URL at import.
TEST_DATABASE_URL = _resolve_test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("ENVIRONMENT", "test")

from app.funding.models import FundingIntent  # noqa: E402
from app.funding.states import FundingState  # noqa: E402
from app.issuers import registry  # noqa: E402
from app.issuers.evm_deposit_mock import EvmDepositMockAdapter  # noqa: E402
from tests.support import SeedIntent  # noqa: E402

TRUNCATED_TABLES = "ledger_events, funding_intents"


async def _create_database_if_missing(url_str: str) -> None:
    url = make_url(url_str)
    target = url.database
    assert target, "test database URL must name a database"
    admin = create_async_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target}"'))
    finally:
        await admin.dispose()


def _run_migrations(url_str: str) -> None:
    from alembic.config import Config

    from alembic import command

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url_str)
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> str:
    """Create + migrate the test database once per session."""
    asyncio.run(_create_database_if_missing(TEST_DATABASE_URL))
    _run_migrations(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def engine(migrated_database: str) -> Iterator[AsyncEngine]:
    # NullPool: pytest-asyncio gives each test its own event loop, and asyncpg
    # connections are bound to the loop that opened them.
    created = create_async_engine(migrated_database, poolclass=NullPool)
    yield created
    asyncio.run(created.dispose())


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: `advance()` commits and returns the intent; callers
    # must be able to read its attributes without another round trip.
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    # TRUNCATE rather than DELETE: the ledger's append-only trigger blocks DELETE.
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {TRUNCATED_TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as db_session:
        yield db_session


@pytest.fixture(autouse=True)
def isolated_issuer_registry() -> Iterator[None]:
    """Restore the adapter registry, and hand every test a fresh provider.

    The mock adapter keeps its simulator in memory, so without this a card
    created in one test would still exist in the next.
    """
    snapshot = dict(registry._FACTORIES)
    registry.reset_instances()
    try:
        yield
    finally:
        registry._FACTORIES.clear()
        registry._FACTORIES.update(snapshot)
        registry.reset_instances()


@pytest.fixture
def mock_adapter() -> EvmDepositMockAdapter:
    """The registered mock provider — the same instance the receiver resolves."""
    adapter = registry.get_adapter("evm_deposit_mock")
    assert isinstance(adapter, EvmDepositMockAdapter)
    return adapter


@pytest.fixture
def seed_intent(session: AsyncSession) -> SeedIntent:
    async def _seed(
        *,
        state: FundingState = FundingState.PENDING,
        amount_minor: int = 2500,
        currency: str = "USD",
        provider_id: str = "evm_deposit_mock",
        card_id: str = "card_test_1",
        deposit_tx_ref: str | None = None,
        retry_count: int = 0,
    ) -> FundingIntent:
        now = datetime.now(UTC)
        intent = FundingIntent(
            state=state,
            provider_id=provider_id,
            card_id=card_id,
            amount_minor=amount_minor,
            currency=currency,
            deposit_tx_ref=deposit_tx_ref,
            retry_count=retry_count,
            state_changed_at=now,
        )
        session.add(intent)
        await session.commit()
        return intent

    return _seed
