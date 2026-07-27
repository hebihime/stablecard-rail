"""Our own reveal token: short-lived, single-use, and not stored (SPEC.md §9.2).

The layer above the provider's. `app/issuers/` mints and redeems Gnosis Pay's PSE
ephemeral token entirely inside one adapter call; this is the token *our* backend
hands a client, and it is the one SPEC.md §9.2 actually describes: "a short-lived,
single-use reveal token from the backend".

Three properties, and each is here because the obvious implementation gets it wrong:

* **Single use has to be atomic.** Read-then-delete lets two requests arriving
  together both win, which is precisely the race a single-use token exists to lose.
* **The token is a bearer credential, so what is stored is a hash of it.** Anyone
  reading Redis — a dump, a `MONITOR`, an operator with a console — holds working
  tokens otherwise, and the store gains nothing from keeping the original.
* **"Replayed" and "never existed" are different incidents**, and both are a 404 to
  the client. Distinguishing them for the ledger costs one marker key; leaking the
  difference over HTTP would tell an attacker when they had guessed a real token.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from app.reveal.store import (
    RevealGrant,
    RevealRejection,
    RevealRejectionReason,
    RevealTokenStore,
    spent_key,
    token_key,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(redis_client: Redis) -> RevealTokenStore:
    return RevealTokenStore(redis_client, ttl_seconds=60, replay_memory_seconds=900)


async def test_minting_returns_a_token_and_its_deadline(store: RevealTokenStore) -> None:
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    assert minted.provider_id == "gnosis_pay_mock"
    assert minted.card_id == "card_1"
    assert minted.expires_at == NOW + timedelta(seconds=60)
    assert minted.expires_in == 60
    # Long enough that guessing is not a strategy. `token_urlsafe(32)` is 43 chars.
    assert len(minted.token) >= 40


async def test_two_mints_never_collide(store: RevealTokenStore) -> None:
    first = await store.mint("gnosis_pay_mock", "card_1", now=NOW)
    second = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    assert first.token != second.token
    # And the first is still redeemable: minting a second token for the same card
    # is a cardholder opening the screen twice, not a reason to revoke.
    assert isinstance(await store.redeem(first.token), RevealGrant)


async def test_the_token_itself_is_never_stored(
    store: RevealTokenStore, redis_client: Redis
) -> None:
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    assert await redis_client.get(f"reveal:token:{minted.token}") is None
    digest = hashlib.sha256(minted.token.encode()).hexdigest()
    assert await redis_client.get(token_key(digest)) is not None


async def test_redeeming_returns_what_the_token_was_minted_for(store: RevealTokenStore) -> None:
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    grant = await store.redeem(minted.token)

    assert isinstance(grant, RevealGrant)
    assert grant.provider_id == "gnosis_pay_mock"
    assert grant.card_id == "card_1"


async def test_a_token_works_exactly_once(store: RevealTokenStore) -> None:
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    assert isinstance(await store.redeem(minted.token), RevealGrant)

    second = await store.redeem(minted.token)
    assert second == RevealRejection(
        reason=RevealRejectionReason.REPLAYED, provider_id="gnosis_pay_mock", card_id="card_1"
    )


async def test_a_replay_is_told_apart_from_a_token_that_never_existed(
    store: RevealTokenStore,
) -> None:
    # The distinction the ledger needs: one of these is someone re-sending a request,
    # the other is someone trying tokens. The API answers 404 to both.
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)
    await store.redeem(minted.token)

    replayed = await store.redeem(minted.token)
    guessed = await store.redeem("never-minted-anywhere")

    assert isinstance(replayed, RevealRejection)
    assert isinstance(guessed, RevealRejection)
    assert replayed.reason is RevealRejectionReason.REPLAYED
    assert guessed.reason is RevealRejectionReason.UNKNOWN
    # A replay is attributable because the spent marker kept the grant; a guess
    # names nothing, and nothing is invented for it.
    assert replayed.card_id == "card_1"
    assert guessed.card_id is None and guessed.provider_id is None


async def test_an_expired_token_is_gone_rather_than_replayed(
    store: RevealTokenStore, redis_client: Redis
) -> None:
    """Redis expiry is what enforces the deadline, so this asserts the TTL is set.

    Sleeping for 60 seconds to watch it happen would be the same assertion, an
    order of magnitude slower — `webhooks/retry.py` makes the same argument.
    """
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    digest = hashlib.sha256(minted.token.encode()).hexdigest()
    assert 0 < await redis_client.ttl(token_key(digest)) <= 60

    await redis_client.delete(token_key(digest))
    rejected = await store.redeem(minted.token)
    assert isinstance(rejected, RevealRejection)
    assert rejected.reason is RevealRejectionReason.UNKNOWN


async def test_the_spent_marker_expires_too(store: RevealTokenStore, redis_client: Redis) -> None:
    # Remembering every token ever spent would be an unbounded set with no reader
    # after the first few minutes. Long enough to catch a replay, then forgotten.
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)
    await store.redeem(minted.token)

    digest = hashlib.sha256(minted.token.encode()).hexdigest()
    assert 0 < await redis_client.ttl(spent_key(digest)) <= 900


async def test_a_grant_carries_no_token(store: RevealTokenStore) -> None:
    # What the API route holds after redemption should not be re-sendable, and a
    # grant that carried its token back would make the whole exchange decorative.
    minted = await store.mint("gnosis_pay_mock", "card_1", now=NOW)

    grant = await store.redeem(minted.token)

    assert isinstance(grant, RevealGrant)
    assert minted.token not in grant.model_dump_json()


@pytest.mark.parametrize(
    ("ttl_seconds", "replay_memory_seconds"),
    [(0, 900), (-1, 900), (60, 0), (60, -1)],
)
async def test_a_non_positive_lifetime_is_refused_at_construction(
    redis_client: Redis, ttl_seconds: int, replay_memory_seconds: int
) -> None:
    # `SET ... ex=0` is an error from Redis, so the alternative to failing here is
    # failing at the first reveal of the day, a long way from the setting that
    # caused it.
    with pytest.raises(ValueError, match="positive"):
        RevealTokenStore(
            redis_client, ttl_seconds=ttl_seconds, replay_memory_seconds=replay_memory_seconds
        )
