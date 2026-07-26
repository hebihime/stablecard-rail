"""Creating funding intents (SPEC.md §5.2 step 1)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.funding.machine import create_intent
from app.funding.states import FundingState
from app.ledger import event_types
from tests.support import ledger_for_intent


async def test_new_intent_starts_pending_and_is_ledgered(session: AsyncSession) -> None:
    intent = await create_intent(
        session,
        provider_id="gnosis_pay_mock",
        card_id="card_abc",
        amount=Money(2500, "USD"),
    )

    assert intent.state is FundingState.PENDING
    assert intent.retry_count == 0
    assert intent.money == Money(2500, "USD")

    events = await ledger_for_intent(session, intent.id)
    assert len(events) == 1
    assert events[0].event_type == event_types.INTENT_CREATED
    assert events[0].state_before is None
    assert events[0].state_after == FundingState.PENDING
    assert events[0].amount_minor == 2500
    assert events[0].currency == "USD"


async def test_amount_is_persisted_as_integer_minor_units(session: AsyncSession) -> None:
    intent = await create_intent(
        session,
        provider_id="gnosis_pay_mock",
        card_id="card_abc",
        amount=Money(1, "USD"),
    )
    assert isinstance(intent.amount_minor, int)
    assert intent.amount_minor == 1


async def test_zero_or_negative_funding_amounts_are_rejected(session: AsyncSession) -> None:
    for bad in (0, -100):
        with pytest.raises(ValueError, match="positive"):
            await create_intent(
                session,
                provider_id="gnosis_pay_mock",
                card_id="card_abc",
                amount=Money(bad, "USD"),
            )
        await session.rollback()


async def test_deposit_reference_is_unique_so_a_replayed_deposit_makes_one_intent(
    session: AsyncSession,
) -> None:
    await create_intent(
        session,
        provider_id="gnosis_pay_mock",
        card_id="card_abc",
        amount=Money(2500, "USD"),
        deposit_tx_ref="5xTxSignature",
    )
    with pytest.raises(IntegrityError):
        await create_intent(
            session,
            provider_id="gnosis_pay_mock",
            card_id="card_abc",
            amount=Money(2500, "USD"),
            deposit_tx_ref="5xTxSignature",
        )


async def test_identifiers_are_stored_opaquely(session: AsyncSession) -> None:
    intent = await create_intent(
        session,
        provider_id="stripe_issuing",
        card_id="ic_1PfakeCardId",
        amount=Money(999, "eur"),
        deposit_tx_ref="4mKp9",
    )
    assert intent.card_id == "ic_1PfakeCardId"
    assert intent.provider_id == "stripe_issuing"
    assert intent.deposit_tx_ref == "4mKp9"
    assert intent.currency == "EUR"


async def test_a_new_intent_has_no_bridged_amount_yet(session: AsyncSession) -> None:
    intent = await create_intent(
        session,
        provider_id="gnosis_pay_mock",
        card_id="card_abc",
        amount=Money(2500, "USD"),
    )

    # `None` means the bridge has not reported, which is not the same as "the
    # bridge delivered nothing" and not the same as "it delivered all of it".
    assert intent.bridged_amount_minor is None
    assert intent.fundable_money == Money(2500, "USD")


async def test_the_card_is_funded_with_what_survived_the_bridge(session: AsyncSession) -> None:
    # SPEC.md §11: bridged amounts arrive net of fees. Funding a card with the
    # deposited amount would be funding it with the bridge's fee as well.
    intent = await create_intent(
        session,
        provider_id="gnosis_pay_mock",
        card_id="card_abc",
        amount=Money(2500, "USD"),
    )
    intent.bridged_amount_minor = 2350
    await session.commit()

    assert intent.money == Money(2500, "USD")  # what was deposited, unchanged
    assert intent.fundable_money == Money(2350, "USD")  # what there is to fund with
