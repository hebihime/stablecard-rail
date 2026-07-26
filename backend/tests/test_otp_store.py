"""The OTP store (SPEC.md §6.2): Redis, keyed to card + challenge, short TTL.

Three properties this store exists to have, and every test below is one of them:

**It expires.** The code is the only value in the system with a deadline, and the
deadline comes from Redis rather than from a sweeper we have to remember to run —
a code that outlives its process is a code that outlives its challenge.

**It is create-only.** A handler can run twice: dispatch fails, the retry queue
re-runs it, another worker drains the same item. The code the app is already
showing must win, because the cardholder has read it and is typing it in. So a
second write is refused rather than merged, and the caller is told which happened.

**It is answerable without a scan.** `GET /otp/pending` needs "what is open right
now", and `KEYS`/`SCAN` over a live keyspace is both unbounded and racy. A sorted
set scored by expiry answers it in one call and prunes itself on the way past.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from app.otp.store import OtpChallenge, OtpStore, Remembered, challenge_key

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def challenge(
    *,
    challenge_id: str = "3ds_000001",
    provider_id: str = "gnosis_pay_mock",
    card_id: str | None = "card_1",
    code: str = "918273",
    expires_in: int = 300,
) -> OtpChallenge:
    return OtpChallenge(
        provider_id=provider_id,
        challenge_id=challenge_id,
        card_id=card_id,
        cardholder_id="user_1",
        event_id="evt-1",
        code=code,
        delivered_at=NOW,
        expires_at=NOW + timedelta(seconds=expires_in),
    )


@pytest.fixture
def store(redis_client: Redis) -> OtpStore:
    return OtpStore(redis_client)


# ------------------------------------------------------------ remembering ----


async def test_a_challenge_is_stored_and_read_back_whole(store: OtpStore) -> None:
    assert Remembered.STORED is await store.remember(challenge(), now=NOW)

    found = await store.get("gnosis_pay_mock", "3ds_000001")
    assert found is not None
    assert "918273" == found.code
    assert "card_1" == found.card_id
    assert NOW + timedelta(seconds=300) == found.expires_at


async def test_an_unknown_challenge_reads_as_nothing(store: OtpStore) -> None:
    assert await store.get("gnosis_pay_mock", "no-such-challenge") is None


async def test_the_key_is_namespaced_by_provider(store: OtpStore) -> None:
    # Two providers numbering their challenges from 1 is normal. Colliding on the
    # id alone would show one provider's code for another's challenge.
    await store.remember(challenge(provider_id="a", code="111111"), now=NOW)
    await store.remember(challenge(provider_id="b", code="222222"), now=NOW)

    first = await store.get("a", "3ds_000001")
    second = await store.get("b", "3ds_000001")
    assert first is not None and second is not None
    assert "111111" == first.code
    assert "222222" == second.code


async def test_a_second_write_does_not_replace_the_code_the_app_is_showing(
    store: OtpStore,
) -> None:
    # The idempotency rule. A retried handler must not swap the code mid-challenge:
    # the cardholder has already read the first one.
    await store.remember(challenge(code="111111"), now=NOW)

    outcome = await store.remember(challenge(code="222222"), now=NOW)

    assert Remembered.ALREADY_KNOWN is outcome
    found = await store.get("gnosis_pay_mock", "3ds_000001")
    assert found is not None
    assert "111111" == found.code


async def test_a_challenge_that_has_already_expired_is_not_stored(store: OtpStore) -> None:
    # Arrives dead: a retry drained long after the challenge, or a provider clock
    # far behind ours. Storing it would put a useless code in front of the
    # cardholder and Redis would reject a non-positive TTL anyway.
    outcome = await store.remember(challenge(expires_in=-1), now=NOW)

    assert Remembered.EXPIRED is outcome
    assert await store.get("gnosis_pay_mock", "3ds_000001") is None


async def test_the_ttl_is_the_time_left_on_the_challenge(
    store: OtpStore, redis_client: Redis
) -> None:
    # Redis owns the expiry, not a sweeper of ours: the code disappears even if
    # nothing ever runs again.
    await store.remember(challenge(expires_in=42), now=NOW)

    assert 42 == await redis_client.ttl(challenge_key("gnosis_pay_mock", "3ds_000001"))


async def test_a_sub_second_remainder_still_buys_a_whole_second(
    store: OtpStore, redis_client: Redis
) -> None:
    # `EX` takes integer seconds, so a 0.4s remainder truncates to zero — which
    # Redis rejects, and which would drop a challenge that is still (just) alive.
    outcome = await store.remember(challenge(expires_in=1), now=NOW + timedelta(milliseconds=600))

    assert Remembered.STORED is outcome
    assert 1 == await redis_client.ttl(challenge_key("gnosis_pay_mock", "3ds_000001"))


# ---------------------------------------------------------------- pending ----


async def test_pending_lists_what_is_open_now(store: OtpStore) -> None:
    await store.remember(challenge(challenge_id="a"), now=NOW)
    await store.remember(challenge(challenge_id="b"), now=NOW)

    pending = await store.pending(now=NOW)

    assert ["a", "b"] == [item.challenge_id for item in pending]


async def test_pending_orders_by_which_challenge_dies_first(store: OtpStore) -> None:
    # The one about to expire is the one the cardholder needs to see first.
    await store.remember(challenge(challenge_id="late", expires_in=300), now=NOW)
    await store.remember(challenge(challenge_id="soon", expires_in=30), now=NOW)

    pending = await store.pending(now=NOW)

    assert ["soon", "late"] == [item.challenge_id for item in pending]


async def test_pending_can_be_narrowed_to_one_card(store: OtpStore) -> None:
    await store.remember(challenge(challenge_id="a", card_id="card_1"), now=NOW)
    await store.remember(challenge(challenge_id="b", card_id="card_2"), now=NOW)

    pending = await store.pending(now=NOW, card_id="card_2")

    assert ["b"] == [item.challenge_id for item in pending]


async def test_pending_excludes_a_challenge_whose_deadline_has_passed(store: OtpStore) -> None:
    await store.remember(challenge(expires_in=30), now=NOW)

    assert [] == await store.pending(now=NOW + timedelta(seconds=31))


async def test_pending_prunes_the_index_when_redis_has_dropped_the_code(
    store: OtpStore, redis_client: Redis
) -> None:
    """The index and the codes expire independently, so they can disagree.

    The code carries its own TTL and the sorted-set member does not — Redis has no
    per-member expiry. So an evicted or manually-deleted code leaves an entry
    pointing at nothing. Reading pending is where that gets tidied up, because it
    is the only operation that finds out.
    """
    await store.remember(challenge(), now=NOW)
    await redis_client.delete(challenge_key("gnosis_pay_mock", "3ds_000001"))

    assert [] == await store.pending(now=NOW)
    # And the stale index entry is gone rather than being rediscovered every poll.
    assert 0 == await redis_client.zcard(store.index_key)


async def test_pending_is_capped(store: OtpStore) -> None:
    for index in range(5):
        await store.remember(challenge(challenge_id=f"c{index}", expires_in=30 + index), now=NOW)

    pending = await store.pending(now=NOW, limit=2)

    assert ["c0", "c1"] == [item.challenge_id for item in pending]


# ---------------------------------------------------------------- forgetting ----


async def test_answering_a_challenge_consumes_it(store: OtpStore, redis_client: Redis) -> None:
    # Single-use, per SPEC.md §6.5: a challenge that has been responded to is no
    # longer pending, and its code is no longer anywhere.
    await store.remember(challenge(), now=NOW)

    assert await store.forget("gnosis_pay_mock", "3ds_000001") is True

    assert await store.get("gnosis_pay_mock", "3ds_000001") is None
    assert 0 == await redis_client.zcard(store.index_key)


async def test_forgetting_something_absent_says_so(store: OtpStore) -> None:
    # The route needs to tell "answered twice" from "answered once", and this is
    # the only place that knows.
    assert await store.forget("gnosis_pay_mock", "3ds_000001") is False


async def test_the_index_is_tidied_even_when_the_code_has_already_expired(
    store: OtpStore, redis_client: Redis
) -> None:
    await store.remember(challenge(), now=NOW)
    await redis_client.delete(challenge_key("gnosis_pay_mock", "3ds_000001"))

    assert await store.forget("gnosis_pay_mock", "3ds_000001") is False
    assert 0 == await redis_client.zcard(store.index_key)
