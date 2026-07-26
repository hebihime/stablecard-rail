"""The OTP consumer (SPEC.md §6.1, §6.2): a challenge webhook becomes a stored code.

The second consumer on the `EventBus`. Phase 2 built the pipe and registered
nothing; phase 5 connected funding's settlement handler; this is OTP's.

Two paths, and both are real:

* the provider sends a code (the mock, which models the ACS-orchestrated shape) —
  we **extract** it;
* the provider sends none (Lithic, whose customer-orchestrated flow makes the card
  program the party that issues the challenge) — we **mint** one.

SPEC.md §6.2 says "extracts/derives" and that is why. `derived` records which
happened, because it changes what the code *is*: a value to relay, or a value we
will have to verify ourselves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.money import Money
from app.core.time import utcnow
from app.issuers.base import CardEvent, CardEventType
from app.ledger import event_types
from app.main import create_app
from app.otp.push import subscription
from app.otp.service import (
    HANDLER_NAME,
    deliver_challenge,
    make_challenge_handler,
    subscribe_challenges,
)
from app.otp.store import OtpChallenge, OtpStore, Remembered
from app.webhooks import dispatch
from tests.support import all_ledger_events

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
CODE = "918273"


def challenge_event(**overrides: object) -> CardEvent:
    fields: dict[str, object] = {
        "provider_id": "gnosis_pay_mock",
        "event_id": "evt-1",
        "event_type": CardEventType.THREE_DS_CHALLENGE,
        "occurred_at": NOW,
        "card_id": "card_1",
        "cardholder_id": "user_1",
        "challenge_id": "3ds_000001",
        "challenge_expires_at": NOW + timedelta(minutes=5),
        "otp_code": CODE,
        "amount": Money(1234, "USD"),
    }
    fields.update(overrides)
    return CardEvent(**fields)  # type: ignore[arg-type]


@pytest.fixture
def store(redis_client: Redis) -> OtpStore:
    return OtpStore(redis_client)


# ------------------------------------------------------- extract or derive ----


async def test_a_code_the_provider_sent_is_the_code_we_store(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    delivery = await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

    assert Remembered.STORED is delivery.outcome
    assert CODE == delivery.challenge.code
    assert delivery.challenge.derived is False
    stored = await store.get("gnosis_pay_mock", "3ds_000001")
    assert stored is not None
    assert CODE == stored.code


async def test_a_provider_that_sends_no_code_gets_one_minted(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    # Lithic's flow: "your organization delivers the challenge to the cardholder
    # through your chosen channel". There is no code to extract because the card
    # program is the party that makes one.
    delivery = await deliver_challenge(
        session, redis_client, challenge_event(otp_code=None), now=NOW
    )

    assert delivery.challenge.derived is True
    assert 6 == len(delivery.challenge.code)
    assert delivery.challenge.code.isdigit()


async def test_minted_codes_are_not_predictable(session: AsyncSession, redis_client: Redis) -> None:
    # A guessable code is not a second factor. `secrets`, not `random`.
    codes = set()
    for index in range(20):
        delivery = await deliver_challenge(
            session,
            redis_client,
            challenge_event(otp_code=None, challenge_id=f"3ds_{index}", event_id=f"evt-{index}"),
            now=NOW,
        )
        codes.add(delivery.challenge.code)

    assert len(codes) > 1


# -------------------------------------------------------------- deadlines ----


async def test_the_challenge_expires_when_the_provider_says(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    delivery = await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

    assert NOW + timedelta(minutes=5) == delivery.challenge.expires_at


async def test_a_challenge_the_provider_did_not_date_gets_the_configured_ttl(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    delivery = await deliver_challenge(
        session, redis_client, challenge_event(challenge_expires_at=None), now=NOW
    )

    assert NOW + timedelta(seconds=300) == delivery.challenge.expires_at


async def test_an_absurd_deadline_is_capped(session: AsyncSession, redis_client: Redis) -> None:
    # A backstop, not a policy about challenge length: a payload claiming the
    # challenge lives for a week would otherwise keep a secret in Redis for a week.
    delivery = await deliver_challenge(
        session,
        redis_client,
        challenge_event(challenge_expires_at=NOW + timedelta(days=7)),
        now=NOW,
    )

    assert NOW + timedelta(seconds=900) == delivery.challenge.expires_at


async def test_a_challenge_that_arrives_dead_is_recorded_rather_than_served(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    # A retry drained long after the fact, or a provider clock well behind ours.
    # Nothing can be done for the cardholder, and the ledger is where "a challenge
    # arrived and we could not serve it" has to be visible.
    delivery = await deliver_challenge(
        session,
        redis_client,
        challenge_event(challenge_expires_at=NOW - timedelta(seconds=1)),
        now=NOW,
    )

    assert Remembered.EXPIRED is delivery.outcome
    assert await store.get("gnosis_pay_mock", "3ds_000001") is None
    entries = await all_ledger_events(session)
    assert [event_types.OTP_UNDELIVERABLE] == [entry.event_type for entry in entries]
    assert "expired" == entries[0].payload["reason"]


# ---------------------------------------------------------------- identity ----


async def test_a_challenge_with_no_id_of_its_own_is_keyed_on_the_delivery(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    # SPEC.md §6.2 keys on "card + challenge id". A provider that sends no
    # challenge id still sends an event id, which is unique and already the basis
    # of webhook dedup — so it is the honest fallback rather than a generated one.
    delivery = await deliver_challenge(
        session, redis_client, challenge_event(challenge_id=None), now=NOW
    )

    assert "evt-1" == delivery.challenge.challenge_id
    assert await store.get("gnosis_pay_mock", "evt-1") is not None


# ------------------------------------------------------------------- push ----


async def test_a_stored_challenge_is_pushed_to_whoever_is_listening(
    session: AsyncSession, redis_client: Redis
) -> None:
    async with subscription(redis_client, card_id="card_1") as pubsub:
        await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    assert message is not None
    assert "3ds_000001" == OtpChallenge.model_validate_json(message["data"]).challenge_id


async def test_a_retry_does_not_re_announce_a_challenge_the_app_is_showing(
    session: AsyncSession, redis_client: Redis
) -> None:
    # The push is a notification that a code has appeared. A second handler run
    # produces no new code, so it produces no notification either.
    await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

    async with subscription(redis_client, card_id="card_1") as pubsub:
        await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) is None


async def test_a_challenge_that_arrived_dead_is_not_pushed(
    session: AsyncSession, redis_client: Redis
) -> None:
    async with subscription(redis_client, card_id="card_1") as pubsub:
        await deliver_challenge(
            session,
            redis_client,
            challenge_event(challenge_expires_at=NOW - timedelta(seconds=1)),
            now=NOW,
        )

        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) is None


# ----------------------------------------------------------------- ledger ----


async def test_a_delivered_challenge_is_ledgered_without_its_code(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

    entries = await all_ledger_events(session)
    assert [event_types.OTP_DELIVERED] == [entry.event_type for entry in entries]
    entry = entries[0]
    assert "card_1" == entry.card_id
    assert "gnosis_pay_mock" == entry.provider_id
    assert 1234 == entry.amount_minor
    assert "3ds_000001" == entry.payload["challenge_id"]
    assert entry.payload["derived"] is False
    assert CODE not in json.dumps(entry.payload)


async def test_a_challenge_with_no_amount_is_still_delivered(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    # Lithic's challenge is one: they state the amount as a decimal plus a currency
    # exponent, which their adapter deliberately leaves unnormalized. A challenge
    # is about authenticating a cardholder, not about a figure, so the absence must
    # not stop the code reaching them.
    delivery = await deliver_challenge(session, redis_client, challenge_event(amount=None), now=NOW)

    assert Remembered.STORED is delivery.outcome
    entries = await all_ledger_events(session)
    assert entries[0].amount_minor is None
    assert entries[0].currency is None


async def test_a_retried_handler_keeps_the_first_code_and_ledgers_once(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    """The idempotency test, and the reason the ledger write is attempted twice.

    A handler can be re-run by the retry queue or by a second worker. The store
    refuses the second code — the cardholder is already reading the first. But the
    ledger row must still converge, so a second run that finds the challenge
    already stored re-attempts the row rather than assuming the first run wrote it:
    the first run may have died between the two.
    """
    first = await deliver_challenge(session, redis_client, challenge_event(), now=NOW)
    second = await deliver_challenge(
        session, redis_client, challenge_event(otp_code="000000"), now=NOW
    )

    assert Remembered.STORED is first.outcome
    assert Remembered.ALREADY_KNOWN is second.outcome
    stored = await store.get("gnosis_pay_mock", "3ds_000001")
    assert stored is not None
    assert CODE == stored.code
    assert 1 == len(await all_ledger_events(session))


async def test_a_run_that_died_before_ledgering_still_ledgers_on_retry(
    session: AsyncSession, redis_client: Redis, store: OtpStore
) -> None:
    # The crash window the test above describes, arranged directly: the code landed
    # in Redis and the process died before the ledger row. A retry must produce the
    # row, so the audit record converges even though the store refuses the write.
    await store.remember(
        OtpChallenge(
            provider_id="gnosis_pay_mock",
            challenge_id="3ds_000001",
            card_id="card_1",
            event_id="evt-1",
            code=CODE,
            delivered_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        ),
        now=NOW,
    )
    assert [] == await all_ledger_events(session)

    delivery = await deliver_challenge(session, redis_client, challenge_event(), now=NOW)

    assert Remembered.ALREADY_KNOWN is delivery.outcome
    assert [event_types.OTP_DELIVERED] == [
        entry.event_type for entry in await all_ledger_events(session)
    ]


# ------------------------------------------------------------ subscription ----


async def test_the_handler_is_subscribed_under_a_stable_name(
    sessionmaker: async_sessionmaker[AsyncSession], redis_client: Redis
) -> None:
    subscribe_challenges(sessionmaker, redis_client)

    assert (CardEventType.THREE_DS_CHALLENGE, HANDLER_NAME) in dispatch.subscriptions()


async def test_the_handler_stores_a_code_when_dispatch_runs_it(
    sessionmaker: async_sessionmaker[AsyncSession], redis_client: Redis, store: OtpStore
) -> None:
    # The handler reads the real clock — it is the one seam where `now` is not an
    # argument, because dispatch hands it only the event. So the challenge is dated
    # relative to now rather than to this module's fixed NOW, which is in the past
    # and would arrive "already expired".
    handler = make_challenge_handler(sessionmaker, redis_client)

    await handler(challenge_event(challenge_expires_at=utcnow() + timedelta(minutes=5)))

    assert await store.get("gnosis_pay_mock", "3ds_000001") is not None


def test_the_running_app_subscribes_the_consumer() -> None:
    """A consumer can exist, be tested, and be wired to nothing.

    `funding/settlement.py` had twelve passing tests and `create_app()` never
    subscribed it, so in the running service a settlement would have been ledgered
    and then handled by nobody (docs/ARCHITECTURE.md §9.15). No test of the handler
    would have caught it. This one would.
    """
    create_app()

    assert (CardEventType.THREE_DS_CHALLENGE, HANDLER_NAME) in dispatch.subscriptions()
