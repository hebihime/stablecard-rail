"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every mapped class in the service."""


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True, future=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: state-machine writers commit and hand the caller a
    # usable object back, without a second round trip to re-read what they wrote.
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session per request."""
    async with get_sessionmaker()() as session:
        yield session
