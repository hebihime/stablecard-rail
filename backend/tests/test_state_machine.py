"""`advance()` against the database: every legal and every illegal transition.

SPEC.md §5.1 / §10: transitions happen only through `advance()`, which enforces
the table, writes a ledger entry per transition, and raises on illegal moves
(which are themselves ledgered).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.funding.machine import (
    IllegalTransitionError,
    IntentNotFoundError,
    advance,
    get_intent,
)
from app.funding.states import RETRYABLE_STATES, TRANSITIONS, FundingState
from app.ledger import event_types
from tests.support import SeedIntent, ledger_for_intent, reload_intent

LEGAL_PAIRS = [(frm, to) for frm, targets in TRANSITIONS.items() for to in sorted(targets)]
ILLEGAL_PAIRS = [
    (frm, to) for frm in FundingState for to in FundingState if to not in TRANSITIONS[frm]
]


def _pair_id(state: FundingState) -> str:
    return str(state)


def test_the_two_parametrised_sets_cover_the_whole_matrix() -> None:
    assert len(LEGAL_PAIRS) + len(ILLEGAL_PAIRS) == len(FundingState) ** 2
    assert set(LEGAL_PAIRS).isdisjoint(ILLEGAL_PAIRS)


@pytest.mark.parametrize(("frm", "to"), LEGAL_PAIRS, ids=_pair_id)
async def test_legal_transition_is_applied_and_ledgered(
    session: AsyncSession, seed_intent: SeedIntent, frm: FundingState, to: FundingState
) -> None:
    intent = await seed_intent(state=frm)
    before = intent.state_changed_at

    returned = await advance(session, intent.id, to, reason="unit test")

    assert returned.state is to
    persisted = await reload_intent(session, intent.id)
    assert persisted.state is to
    assert persisted.state_changed_at >= before

    events = await ledger_for_intent(session, intent.id)
    assert len(events) == 1
    event = events[0]
    assert event.state_before == frm
    assert event.state_after == to
    assert event.intent_id == intent.id
    assert event.card_id == intent.card_id
    assert event.provider_id == intent.provider_id
    assert event.payload["reason"] == "unit test"
    assert event.occurred_at <= datetime.now(UTC)
    expected_type = event_types.INTENT_RETRIED if frm is to else event_types.INTENT_TRANSITIONED
    assert event.event_type == expected_type


@pytest.mark.parametrize(("frm", "to"), ILLEGAL_PAIRS, ids=_pair_id)
async def test_illegal_transition_raises_and_is_ledgered(
    session: AsyncSession, seed_intent: SeedIntent, frm: FundingState, to: FundingState
) -> None:
    intent = await seed_intent(state=frm)

    with pytest.raises(IllegalTransitionError) as excinfo:
        await advance(session, intent.id, to)

    assert excinfo.value.from_state is frm
    assert excinfo.value.to_state is to
    assert str(frm) in str(excinfo.value) and str(to) in str(excinfo.value)

    # State untouched...
    persisted = await reload_intent(session, intent.id)
    assert persisted.state is frm

    # ...but the attempt survives the failure, committed independently.
    events = await ledger_for_intent(session, intent.id)
    assert len(events) == 1
    assert events[0].event_type == event_types.INTENT_ILLEGAL_TRANSITION
    assert events[0].state_before == frm
    assert events[0].state_after == to


@pytest.mark.parametrize("state", sorted(RETRYABLE_STATES))
async def test_retry_self_transition_increments_retry_count(
    session: AsyncSession, seed_intent: SeedIntent, state: FundingState
) -> None:
    intent = await seed_intent(state=state)
    assert intent.retry_count == 0

    await advance(session, intent.id, state, reason="bridge still pending")
    await advance(session, intent.id, state, reason="bridge still pending")

    persisted = await reload_intent(session, intent.id)
    assert persisted.retry_count == 2
    assert persisted.last_error == "bridge still pending"
    events = await ledger_for_intent(session, intent.id)
    assert [event.event_type for event in events] == [event_types.INTENT_RETRIED] * 2
    assert events[-1].payload["retry_count"] == 2


async def test_forward_transition_does_not_touch_retry_count(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.BRIDGING, retry_count=3)
    await advance(session, intent.id, FundingState.BRIDGED)
    persisted = await reload_intent(session, intent.id)
    assert persisted.retry_count == 3


async def test_failure_reason_is_recorded_on_the_intent(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.FUNDING)
    await advance(session, intent.id, FundingState.FAILED_FUNDING, reason="issuer rejected: 402")
    persisted = await reload_intent(session, intent.id)
    assert persisted.last_error == "issuer rejected: 402"


async def test_happy_path_walk_produces_one_ledger_entry_per_hop(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.PENDING)
    path = [
        FundingState.DEPOSIT_CONFIRMED,
        FundingState.BRIDGING,
        FundingState.BRIDGED,
        FundingState.FUNDING,
        FundingState.FUNDED,
        FundingState.SETTLED,
    ]
    for target in path:
        await advance(session, intent.id, target)

    persisted = await reload_intent(session, intent.id)
    assert persisted.state is FundingState.SETTLED
    events = await ledger_for_intent(session, intent.id)
    assert [event.state_after for event in events] == [str(state) for state in path]
    # Ledger ids are monotonic, so a walk-through reads in order.
    assert [event.id for event in events] == sorted(event.id for event in events)


async def test_advance_on_unknown_intent_raises(session: AsyncSession) -> None:
    with pytest.raises(IntentNotFoundError):
        await advance(session, uuid.uuid4(), FundingState.DEPOSIT_CONFIRMED)


async def test_get_intent_reads_back_committed_state(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.FUNDING, retry_count=2)
    fetched = await get_intent(session, intent.id)
    assert fetched.id == intent.id
    assert fetched.state is FundingState.FUNDING
    assert fetched.retry_count == 2


async def test_get_intent_on_unknown_id_raises(session: AsyncSession) -> None:
    with pytest.raises(IntentNotFoundError):
        await get_intent(session, uuid.uuid4())


async def test_extra_payload_is_recorded_as_ledger_context(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.BRIDGED)
    await advance(
        session,
        intent.id,
        FundingState.FUNDING,
        payload={"issuer_request_id": "req_1", "attempt": 1},
    )
    events = await ledger_for_intent(session, intent.id)
    assert events[0].payload["context"] == {"issuer_request_id": "req_1", "attempt": 1}


async def test_advance_can_update_allowlisted_reference_fields(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.DEPOSIT_CONFIRMED)
    await advance(
        session,
        intent.id,
        FundingState.BRIDGING,
        updates={"bridge_ref": "0xbridge-order-1"},
    )
    persisted = await reload_intent(session, intent.id)
    assert persisted.bridge_ref == "0xbridge-order-1"


async def test_the_bridged_amount_is_set_by_the_transition_that_learns_it(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # BRIDGING -> BRIDGED is the moment the delivered amount becomes known, so
    # it is set through `advance()` like every other fact about an intent —
    # in the same transaction as the state change and the ledger entry.
    intent = await seed_intent(state=FundingState.BRIDGING, amount_minor=2500)

    await advance(
        session,
        intent.id,
        FundingState.BRIDGED,
        updates={"bridged_amount_minor": 2350},
    )

    persisted = await reload_intent(session, intent.id)
    assert persisted.amount_minor == 2500  # the deposit, still the deposit
    assert persisted.bridged_amount_minor == 2350
    assert persisted.fundable_money.amount_minor == 2350


@pytest.mark.parametrize("state", [FundingState.DEPOSIT_CONFIRMED, FundingState.BRIDGED])
async def test_the_hand_off_states_can_retry_in_place(
    session: AsyncSession, seed_intent: SeedIntent, state: FundingState
) -> None:
    # Phase 5's addition to the table. A submit or a `fund_card` that fails with
    # a 503 has to be countable, or the retry cap in SPEC.md §5.3 has nothing to
    # count and the intent retries forever (docs/ARCHITECTURE.md §9.9).
    intent = await seed_intent(state=state)

    await advance(session, intent.id, state, reason="provider unavailable")

    persisted = await reload_intent(session, intent.id)
    assert persisted.state is state
    assert persisted.retry_count == 1
    assert persisted.last_error == "provider unavailable"


async def test_advance_refuses_to_update_fields_outside_the_allowlist(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.DEPOSIT_CONFIRMED)
    for field in ("state", "amount_minor", "id", "not_a_column"):
        with pytest.raises(ValueError, match="not updatable"):
            await advance(session, intent.id, FundingState.BRIDGING, updates={field: "x"})
    # Nothing was applied.
    persisted = await reload_intent(session, intent.id)
    assert persisted.state is FundingState.DEPOSIT_CONFIRMED
