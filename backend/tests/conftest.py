"""Test bootstrap.

The suite runs against real PostgreSQL and real Redis (see docs/ARCHITECTURE.md
§2.11): the ledger relies on JSONB, identity columns, partial-unique semantics and
an append-only trigger, and webhook dedup is `SETNX`-with-TTL semantics — none of
which a fake models faithfully, and all of which this system's correctness rests
on. The database is created if missing and migrated with Alembic, so every run
also exercises the migration path.

`DATABASE_URL` and `REDIS_URL` are rewritten to test targets *before* application
modules are imported, so app code, Alembic and the tests can never disagree about
where they point.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
_DEFAULT_REDIS_URL = "redis://localhost:6389/0"
#: The suite flushes this database wholesale, so it must never be the app's.
TEST_REDIS_DB = 15


def _resolve_test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(os.getenv("DATABASE_URL", _DEFAULT_URL))
    # str(URL) masks the password; render explicitly or connections fail.
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _resolve_test_redis_url() -> str:
    explicit = os.getenv("TEST_REDIS_URL")
    if explicit:
        return explicit
    parts = urlsplit(os.getenv("REDIS_URL", _DEFAULT_REDIS_URL))
    return urlunsplit(parts._replace(path=f"/{TEST_REDIS_DB}"))


# Must happen before `app.*` is imported: app settings read the environment at import.
TEST_DATABASE_URL = _resolve_test_database_url()
TEST_REDIS_URL = _resolve_test_redis_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ.setdefault("ENVIRONMENT", "test")

import httpx  # noqa: E402
from redis.asyncio import Redis  # noqa: E402

from app.core.db import get_session  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.funding.models import FundingIntent  # noqa: E402
from app.funding.states import FundingState  # noqa: E402
from app.issuers import registry  # noqa: E402
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter  # noqa: E402
from app.main import create_app  # noqa: E402
from app.webhooks import dispatch  # noqa: E402
from tests.support import SeedIntent  # noqa: E402

TRUNCATED_TABLES = "ledger_events, funding_intents, webhook_dead_letters"


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


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """A flushed Redis on a database of its own.

    Per test, not per session: `redis.asyncio` connections are bound to the event
    loop that opened them, exactly like asyncpg's.
    """
    client: Redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


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


@pytest.fixture(autouse=True)
def isolated_subscriptions() -> Iterator[None]:
    """Webhook handler subscriptions are process-global; do not let them leak."""
    dispatch.clear_subscriptions()
    yield
    dispatch.clear_subscriptions()


@pytest.fixture
def mock_adapter() -> GnosisPayMockAdapter:
    """The registered mock provider — the same instance the receiver resolves."""
    adapter = registry.get_adapter("gnosis_pay_mock")
    assert isinstance(adapter, GnosisPayMockAdapter)
    return adapter


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession], redis_client: Redis
) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, wired to the test database and Redis.

    A session per request, as in production: routes that commit must not depend on
    sharing one session with the test.
    """
    app = create_app()

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as request_session:
            yield request_session

    async def _redis_override() -> AsyncIterator[Redis]:
        yield redis_client

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_redis] = _redis_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stablecard.test") as http:
        yield http


@pytest.fixture
def seed_intent(session: AsyncSession) -> SeedIntent:
    async def _seed(
        *,
        state: FundingState = FundingState.PENDING,
        amount_minor: int = 2500,
        currency: str = "USD",
        provider_id: str = "gnosis_pay_mock",
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
