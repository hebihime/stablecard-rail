"""Webhook idempotency (SPEC.md §4).

Two layers, and only one of them is authoritative:

1. **Redis `SETNX` with a TTL** — the fast path. Cheap, and it absorbs the burst
   of duplicates that arrive when a provider retries a delivery it already sent.
2. **A unique index on `ledger_events.idempotency_key`** — the durable path. This
   is what still holds after a Redis eviction, restart, or TTL expiry.

Claiming before doing the work (rather than after) is what SPEC.md §4 specifies,
and it is the right way round for concurrency: two simultaneous deliveries cannot
both pass. The cost is a crash window — claim, then die before recording, and the
provider's redelivery looks like a duplicate. `release()` closes it: any failure
before commit gives the claim back.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import get_settings

__all__ = ["DedupGate", "dedup_key", "ledger_idempotency_key"]


def dedup_key(provider_id: str, event_id: str) -> str:
    """The Redis key for one delivery.

    Namespaced by provider: two providers both numbering their events from 1 is
    normal, and colliding on `event_id` alone would silently drop traffic.
    """
    return f"webhook:dedup:{provider_id}:{event_id}"


def ledger_idempotency_key(provider_id: str, event_id: str) -> str:
    """The durable key, stored on the ledger row under a unique index."""
    return f"webhook:{provider_id}:{event_id}"


class DedupGate:
    """Exclusive, expiring claims on `(provider_id, event_id)`."""

    def __init__(self, redis: Redis, *, ttl_seconds: int | None = None) -> None:
        self._redis = redis
        self._ttl = ttl_seconds or get_settings().webhook_dedup_ttl_seconds

    async def claim(self, provider_id: str, event_id: str) -> bool:
        """True if this caller may process the delivery; False if someone already has."""
        key = dedup_key(provider_id, event_id)
        return bool(await self._redis.set(key, "1", nx=True, ex=self._ttl))

    async def release(self, provider_id: str, event_id: str) -> None:
        """Give a claim back after failing to complete the work it guarded."""
        await self._redis.delete(dedup_key(provider_id, event_id))
