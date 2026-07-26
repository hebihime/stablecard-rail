"""Replay- and concurrency-safety of `advance()`.

The webhook receiver (phase 2) will call `advance()` from at-least-once delivery,
so a repeated call carrying the same idempotency key must be a no-op rather than
an error, and two racing workers must not both apply a transition.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.funding.machine import IllegalTransitionError, advance
from app.funding.states import FundingState
from tests.support import SeedIntent, ledger_for_intent, reload_intent


async def test_replayed_key_applies_the_transition_once(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.PENDING)

    first = await advance(
        session, intent.id, FundingState.DEPOSIT_CONFIRMED, idempotency_key="delivery-1"
    )
    second = await advance(
        session, intent.id, FundingState.DEPOSIT_CONFIRMED, idempotency_key="delivery-1"
    )

    assert first.state is FundingState.DEPOSIT_CONFIRMED
    assert second.state is FundingState.DEPOSIT_CONFIRMED
    assert len(await ledger_for_intent(session, intent.id)) == 1


async def test_replay_short_circuits_before_the_legality_check(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # The second delivery of the same event would be an illegal repeat of a
    # completed hop; idempotency must win over the transition table, otherwise
    # at-least-once delivery turns into spurious 500s.
    intent = await seed_intent(state=FundingState.BRIDGED)
    await advance(session, intent.id, FundingState.FUNDING, idempotency_key="evt_42")
    await advance(session, intent.id, FundingState.FUNDED, idempotency_key="evt_43")

    replay = await advance(session, intent.id, FundingState.FUNDING, idempotency_key="evt_42")

    assert replay.state is FundingState.FUNDED
    assert len(await ledger_for_intent(session, intent.id)) == 2


async def test_distinct_keys_still_apply_separately(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.BRIDGING)
    await advance(session, intent.id, FundingState.BRIDGING, idempotency_key="retry-1")
    await advance(session, intent.id, FundingState.BRIDGING, idempotency_key="retry-2")
    persisted = await reload_intent(session, intent.id)
    assert persisted.retry_count == 2


async def test_unkeyed_advances_are_never_deduplicated(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    intent = await seed_intent(state=FundingState.FUNDING)
    await advance(session, intent.id, FundingState.FUNDING)
    await advance(session, intent.id, FundingState.FUNDING)
    assert len(await ledger_for_intent(session, intent.id)) == 2


async def test_concurrent_advances_are_serialised_by_row_lock(
    sessionmaker: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    seed_intent: SeedIntent,
) -> None:
    # FUNDED -> SETTLED, because the target is terminal: whichever worker loses
    # the lock finds a state with nowhere left to go, so "applied twice" is
    # unambiguously illegal rather than indistinguishable from a retry. (Phase 5
    # gave DEPOSIT_CONFIRMED a self-loop, which is what this used to race on —
    # the loser there is now a legal retry, which tests the counter, not the lock.)
    intent = await seed_intent(state=FundingState.FUNDED)

    async def hop() -> str:
        async with sessionmaker() as worker:
            try:
                result = await advance(worker, intent.id, FundingState.SETTLED)
            except IllegalTransitionError:
                return "rejected"
            return str(result.state)

    outcomes = await asyncio.gather(hop(), hop())

    assert sorted(outcomes) == ["SETTLED", "rejected"]
    persisted = await reload_intent(session, intent.id)
    assert persisted.state is FundingState.SETTLED
    events = await ledger_for_intent(session, intent.id)
    # One applied transition + one ledgered rejection.
    assert len(events) == 2


async def test_unique_constraint_is_the_backstop_when_the_precheck_misses(
    session: AsyncSession, seed_intent: SeedIntent, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SPEC.md §4: Redis dedup is a fast path; the ledger's unique index is what
    # makes replay safe after eviction. Blinding the pre-check simulates exactly
    # that, and the transition must still apply only once.
    intent = await seed_intent(state=FundingState.BRIDGING)
    await advance(session, intent.id, FundingState.BRIDGING, idempotency_key="evt_dupe")

    async def _blind(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.funding.machine.find_by_idempotency_key", _blind)

    replay = await advance(session, intent.id, FundingState.BRIDGING, idempotency_key="evt_dupe")

    assert replay.retry_count == 1
    assert len(await ledger_for_intent(session, intent.id)) == 1


async def test_unrelated_integrity_errors_are_not_swallowed_as_replays(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    taken = await seed_intent(state=FundingState.PENDING, deposit_tx_ref="sig-1")
    other = await seed_intent(state=FundingState.PENDING, card_id="card_test_2")
    assert taken.deposit_tx_ref == "sig-1"

    with pytest.raises(IntegrityError):
        await advance(
            session,
            other.id,
            FundingState.DEPOSIT_CONFIRMED,
            updates={"deposit_tx_ref": "sig-1"},
            idempotency_key="evt_other",
        )


@pytest.mark.parametrize("key", ["", " "])
async def test_blank_idempotency_keys_are_rejected(
    session: AsyncSession, seed_intent: SeedIntent, key: str
) -> None:
    intent = await seed_intent(state=FundingState.PENDING)
    with pytest.raises(ValueError, match="idempotency_key"):
        await advance(session, intent.id, FundingState.DEPOSIT_CONFIRMED, idempotency_key=key)
