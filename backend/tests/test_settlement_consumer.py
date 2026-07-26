"""The first EventBus consumer: a settlement against a funding intent.

Half of these tests are about the events this consumer refuses to act on, which
is the more important half. A false reconciliation is silent and wrong; no
reconciliation leaves an intent at `FUNDED`, which is simply true.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.money import Money
from app.funding.machine import IllegalTransitionError
from app.funding.settlement import (
    HANDLER_NAME,
    make_settlement_handler,
    settle_from_event,
    subscribe_settlement,
)
from app.funding.states import FundingState
from app.issuers.base import CardEvent, CardEventType
from app.ledger import event_types
from app.webhooks.bus import RedisStreamsEventBus
from app.webhooks.dispatch import dispatch, handlers_for, subscriptions
from app.webhooks.retry import RetryQueue
from tests.support import SeedIntent, ledger_for_intent, reload_intent

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def a_settlement(
    *,
    funding_ref: str | None,
    amount: Money | None = Money(2500, "USD"),
    event_id: str = "evt_settle_1",
) -> CardEvent:
    return CardEvent(
        provider_id="gnosis_pay_mock",
        event_id=event_id,
        event_type=CardEventType.SETTLEMENT,
        occurred_at=NOW,
        provider_event_type="transaction.cleared",
        card_id="card_test_1",
        amount=amount,
        funding_ref=funding_ref,
    )


# --------------------------------------------------------- what it settles ----


async def test_a_settlement_that_names_an_intent_settles_it(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.FUNDED)

    settled = await settle_from_event(session, a_settlement(funding_ref=str(intent.id)))

    assert settled == intent.id
    assert (await reload_intent(session, intent.id)).state is FundingState.SETTLED


async def test_the_provider_event_is_recorded_on_the_transition(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # The ledger has to be able to answer "what settled this?" — the provider's
    # own event id is the only thing that can.
    intent = await seed_intent(state=FundingState.FUNDED)

    await settle_from_event(session, a_settlement(funding_ref=str(intent.id)))

    event = (await ledger_for_intent(session, intent.id))[-1]
    assert event.event_type == event_types.INTENT_TRANSITIONED
    assert event.state_after == str(FundingState.SETTLED)
    assert event.payload["context"]["provider_event_id"] == "evt_settle_1"
    assert event.payload["context"]["provider_event_type"] == "transaction.cleared"
    assert event.payload["context"]["settled_minor"] == 2500


async def test_a_replayed_settlement_settles_once(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # At-least-once delivery is the premise of the whole receiver (SPEC.md §4),
    # so a second copy must not raise out of a terminal state.
    intent = await seed_intent(state=FundingState.FUNDED)

    first = await settle_from_event(session, a_settlement(funding_ref=str(intent.id)))
    second = await settle_from_event(session, a_settlement(funding_ref=str(intent.id)))

    assert first == second == intent.id
    transitions = [
        event
        for event in await ledger_for_intent(session, intent.id)
        if event.state_after == str(FundingState.SETTLED)
    ]
    assert len(transitions) == 1


async def test_a_second_distinct_settlement_for_one_funding_is_also_a_no_op(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # Keyed on the intent, not the delivery: two different provider events for
    # one funding is a provider's business, and neither should raise.
    intent = await seed_intent(state=FundingState.FUNDED)
    await settle_from_event(session, a_settlement(funding_ref=str(intent.id)))

    again = await settle_from_event(
        session, a_settlement(funding_ref=str(intent.id), event_id="evt_settle_2")
    )

    assert again == intent.id
    assert (await reload_intent(session, intent.id)).state is FundingState.SETTLED


# ------------------------------------------------------ what it refuses to ----


async def test_a_settlement_with_no_funding_reference_settles_nothing(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # The common case: `SETTLEMENT` at these providers is a purchase clearing.
    # Matching it to an intent on card and amount would let a $25 coffee settle a
    # $25 top-up — silent, and wrong (§9.12).
    intent = await seed_intent(state=FundingState.FUNDED, amount_minor=2500)

    settled = await settle_from_event(session, a_settlement(funding_ref=None))

    assert settled is None
    assert (await reload_intent(session, intent.id)).state is FundingState.FUNDED
    assert await ledger_for_intent(session, intent.id) == []


async def test_an_unparseable_funding_reference_is_not_guessed_at(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.FUNDED)

    settled = await settle_from_event(session, a_settlement(funding_ref="not-a-uuid"))

    assert settled is None
    assert (await reload_intent(session, intent.id)).state is FundingState.FUNDED


async def test_a_settlement_for_an_intent_we_do_not_have_is_reported_not_raised(
    session: AsyncSession,
) -> None:
    # A provider echoing a reference from another environment, or from a database
    # that has been reset. Nothing to do, and nothing worth retrying.
    settled = await settle_from_event(session, a_settlement(funding_ref=str(uuid.uuid4())))

    assert settled is None


async def test_a_settlement_for_an_intent_that_is_not_funded_yet_raises(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # Something is genuinely wrong: the provider says a funding settled that we
    # do not believe we ever completed. It is ledgered as an illegal transition
    # and then travels the retry-and-dead-letter path (§3.7) rather than being
    # swallowed here.
    intent = await seed_intent(state=FundingState.BRIDGING)

    with pytest.raises(IllegalTransitionError):
        await settle_from_event(session, a_settlement(funding_ref=str(intent.id)))

    events = await ledger_for_intent(session, intent.id)
    assert events[-1].event_type == event_types.INTENT_ILLEGAL_TRANSITION


# -------------------------------------------------------- through the bus ----


async def test_the_handler_is_subscribed_under_a_stable_name(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    subscribe_settlement(sessionmaker)

    assert (CardEventType.SETTLEMENT, HANDLER_NAME) in subscriptions()
    assert [name for name, _ in handlers_for(CardEventType.SETTLEMENT)] == [HANDLER_NAME]


async def test_dispatching_a_settlement_reaches_the_intent(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    seed_intent: SeedIntent,
) -> None:
    # End to end through the phase-2 machinery: published on the bus, handler run
    # by `dispatch`, intent advanced. Phase 2 left this subscription empty on
    # purpose (§3.10) and this is what fills it.
    intent = await seed_intent(state=FundingState.FUNDED)
    subscribe_settlement(sessionmaker)

    report = await dispatch(
        a_settlement(funding_ref=str(intent.id)),
        bus=RedisStreamsEventBus(redis_client),
        retry_queue=RetryQueue(redis_client),
        now=NOW,
    )

    assert report.ran == (HANDLER_NAME,)
    assert report.failed == ()
    assert (await reload_intent(session, intent.id)).state is FundingState.SETTLED


async def test_a_handler_failure_is_queued_for_retry_rather_than_lost(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    seed_intent: SeedIntent,
) -> None:
    # The illegal-transition case, seen from `dispatch`: the provider still gets
    # its 200 (the delivery was authentic), and our problem goes to the queue.
    intent = await seed_intent(state=FundingState.BRIDGING)
    subscribe_settlement(sessionmaker)

    report = await dispatch(
        a_settlement(funding_ref=str(intent.id)),
        bus=RedisStreamsEventBus(redis_client),
        retry_queue=RetryQueue(redis_client),
        now=NOW,
    )

    assert report.failed == (HANDLER_NAME,)
    assert await RetryQueue(redis_client).due(now=NOW.replace(hour=23)) != []


async def test_the_handler_opens_its_own_session(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    seed_intent: SeedIntent,
) -> None:
    # `Handler` takes only the event — that is what keeps `webhooks/` free of the
    # database — so the consumer brings its own session, as a Kafka consumer would.
    intent = await seed_intent(state=FundingState.FUNDED)
    handler = make_settlement_handler(sessionmaker)

    await handler(a_settlement(funding_ref=str(intent.id)))

    assert (await reload_intent(session, intent.id)).state is FundingState.SETTLED
