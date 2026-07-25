"""The Redis half of webhook dedup (SPEC.md §4).

`SETNX` with a TTL, keyed on `(provider_id, event_id)`. This layer is a cache:
the ledger's unique index is what actually guarantees once-only processing. What
must hold here is that a claim is exclusive, expires, and can be given back if the
work it guarded did not complete.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.webhooks.dedup import DedupGate, dedup_key, ledger_idempotency_key


def test_keys_are_namespaced_per_provider() -> None:
    # Two providers legitimately number their events from 1. Colliding on
    # `event_id` alone would drop the second provider's traffic on the floor.
    assert dedup_key("lithic", "evt_1") != dedup_key("stripe_issuing", "evt_1")
    assert ledger_idempotency_key("lithic", "evt_1") != ledger_idempotency_key("stripe", "evt_1")


def test_keys_are_stable_strings() -> None:
    # These end up in Redis and in a database column; drifting format silently
    # disables dedup for in-flight deliveries.
    assert dedup_key("lithic", "evt_1") == "webhook:dedup:lithic:evt_1"
    assert ledger_idempotency_key("lithic", "evt_1") == "webhook:lithic:evt_1"


async def test_the_first_claim_wins_and_the_second_loses(redis_client: Redis) -> None:
    gate = DedupGate(redis_client)
    assert await gate.claim("lithic", "evt_1") is True
    assert await gate.claim("lithic", "evt_1") is False


async def test_a_claim_expires_so_redis_cannot_grow_without_bound(redis_client: Redis) -> None:
    gate = DedupGate(redis_client, ttl_seconds=60)
    await gate.claim("lithic", "evt_1")
    ttl = await redis_client.ttl(dedup_key("lithic", "evt_1"))
    assert 0 < ttl <= 60


async def test_claims_for_different_events_are_independent(redis_client: Redis) -> None:
    gate = DedupGate(redis_client)
    assert await gate.claim("lithic", "evt_1") is True
    assert await gate.claim("lithic", "evt_2") is True


async def test_releasing_a_claim_lets_the_provider_retry(redis_client: Redis) -> None:
    # The crash window SPEC.md §4's SETNX-first ordering opens: if we claim and
    # then fail before the ledger write, the provider's redelivery must not be
    # mistaken for a duplicate. Releasing on failure closes it.
    gate = DedupGate(redis_client)
    await gate.claim("lithic", "evt_1")
    await gate.release("lithic", "evt_1")
    assert await gate.claim("lithic", "evt_1") is True


async def test_releasing_an_unclaimed_event_is_harmless(redis_client: Redis) -> None:
    await DedupGate(redis_client).release("lithic", "never-seen")


async def test_the_ttl_comes_from_settings_by_default(redis_client: Redis) -> None:
    from app.core.config import get_settings

    gate = DedupGate(redis_client)
    await gate.claim("lithic", "evt_1")
    ttl = await redis_client.ttl(dedup_key("lithic", "evt_1"))
    assert ttl > get_settings().webhook_dedup_ttl_seconds - 10
