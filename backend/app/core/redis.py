"""Redis client and FastAPI dependency.

Redis carries the volatile half of the system (SPEC.md §2): webhook dedup claims,
the handler retry queue, the `EventBus` stream, and — from phase 7 — OTP codes.
Nothing durable lives here: every fact Redis holds is either reconstructible from
Postgres or deliberately short-lived.

`decode_responses=True`: every value this service stores is UTF-8 JSON or a flag,
so decoding at the boundary keeps `str | bytes` unions out of the call sites.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache
def get_redis_client() -> Redis:
    """Process-wide client. Redis multiplexes, so one client is the right number."""
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def get_redis() -> AsyncIterator[Redis]:
    """FastAPI dependency. The client outlives the request; it is not closed here."""
    yield get_redis_client()
