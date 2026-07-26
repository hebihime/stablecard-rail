"""The auto top-up engine (SPEC.md §5.2 steps 2-3, §10).

One intent, one hop, one outside call. What is being asserted throughout is not
just "the happy path works" but the three-way split every failure goes through:
retry in place, fail the intent, or leave it alone entirely — because two of
those are irreversible and the third is not.

The provider here is a fake registered in the real registry, so the engine
resolves it exactly as it resolves Lithic or Stripe. The bridge is the simulator,
which is deterministic (docs/ARCHITECTURE.md §9.4).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.bridge.simulated import BridgeFailureMode, SimulatedBridge
from app.core.money import Money
from app.funding.engine import StepOutcome, TopUpEngine, TopUpPolicy
from app.funding.machine import advance
from app.funding.states import FundingState
from app.issuers import registry
from app.issuers.base import (
    Card,
    CardEvent,
    Cardholder,
    CardIssuerAdapter,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingResult,
    FundingStatus,
    IssuerError,
)
from app.ledger import event_types
from tests.support import SeedIntent, ledger_for_intent, reload_intent

SAFE = "0xSafe000000000000000000000000000000000001"
SETTLEMENT = "0xTreasury00000000000000000000000000000001"
START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)


class FakeIssuer(CardIssuerAdapter):
    """A provider whose every answer the test chooses.

    Registered under a `provider_id` of its own, so the engine reaches it the
    only way it reaches any adapter: `registry.get_adapter(intent.provider_id)`.
    """

    def __init__(
        self,
        *,
        provider_id: str = "fake_issuer",
        funding_model: FundingModel = FundingModel.CRYPTO_DEPOSIT,
        deposit_address: str | None = SAFE,
        status: FundingStatus = FundingStatus.SUCCEEDED,
        raises: Exception | None = None,
        get_card_raises: Exception | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.funding_model = funding_model
        self.deposit_address = deposit_address
        self.status = status
        self.raises = raises
        self.get_card_raises = get_card_raises
        #: Every `fund_card` call, so idempotency can be asserted on the ref.
        self.funded: list[tuple[str, Money, str]] = []

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        raise NotImplementedError

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        raise NotImplementedError

    async def get_card(self, card_id: str) -> Card:
        if self.get_card_raises is not None:
            raise self.get_card_raises
        return Card(
            provider_id=self.provider_id,
            card_id=card_id,
            cardholder_id="ch_1",
            state=CardState.ACTIVE,
            last_four="4242",
            exp_month=12,
            exp_year=2030,
            currency="USD",
            deposit_address=self.deposit_address,
            created_at=START,
        )

    async def activate_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def freeze_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def cancel_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        self.funded.append((card_id, amount, funding_ref))
        if self.raises is not None:
            raise self.raises
        return FundingResult(
            provider_id=self.provider_id,
            card_id=card_id,
            funding_ref=funding_ref,
            issuer_funding_ref="prov_fund_1" if self.status is FundingStatus.SUCCEEDED else None,
            status=self.status,
            amount=amount,
        )

    async def get_balance(self, card_id: str) -> Money:
        raise NotImplementedError

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return True

    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        raise NotImplementedError


@pytest.fixture
def issuer() -> FakeIssuer:
    """A fake provider, registered the way a real adapter is."""
    adapter = FakeIssuer()
    registry.register("fake_issuer", lambda: adapter, replace=True)
    return adapter


def an_engine(bridge: SimulatedBridge | None = None, **policy: object) -> TopUpEngine:
    return TopUpEngine(
        bridge or SimulatedBridge(clock=Clock()),
        policy=TopUpPolicy(settlement_address=SETTLEMENT, **policy),  # type: ignore[arg-type]
    )


async def an_intent(seed_intent: SeedIntent, state: FundingState, **overrides: object) -> uuid.UUID:
    intent = await seed_intent(state=state, provider_id="fake_issuer", **overrides)  # type: ignore[arg-type]
    return intent.id


# ------------------------------------------------- DEPOSIT_CONFIRMED -> ... --


async def test_a_confirmed_deposit_is_submitted_to_the_bridge(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.BRIDGING
    assert outcome.progressed
    persisted = await reload_intent(session, intent_id)
    assert persisted.bridge_ref  # stored, so a restart can ask about it
    events = await ledger_for_intent(session, intent_id)
    assert events[-1].event_type == event_types.INTENT_TRANSITIONED
    assert events[-1].state_after == str(FundingState.BRIDGING)


async def test_the_bridge_order_is_keyed_on_the_intent_id(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # The engine retries a submit it could not confirm, so the key is what stops
    # one deposit from becoming two bridge orders.
    bridge = SimulatedBridge(clock=Clock())
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    await an_engine(bridge).step(session, intent_id)
    transfer = await bridge.status(f"sim_{intent_id}")

    assert transfer.order_ref == str(intent_id)


async def test_a_crypto_deposit_issuer_is_paid_at_the_cards_own_address(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # §9.3: for a CRYPTO_DEPOSIT provider the bridge delivers to the card's Safe,
    # and arrival *is* the funding. The address is read from the provider, not
    # from a local copy of it (§3.4).
    bridge = SimulatedBridge(clock=Clock())
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    await an_engine(bridge).step(session, intent_id)

    transfer = await bridge.status(f"sim_{intent_id}")
    assert transfer.raw["destination_address"] == SAFE
    events = await ledger_for_intent(session, intent_id)
    assert events[-1].payload["context"]["destination_address"] == SAFE


async def test_a_fiat_rail_issuer_is_paid_at_our_settlement_address(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # A fiat rail has no on-chain address, so the bridge delivers to us and the
    # funding is a separate API call.
    issuer.funding_model = FundingModel.FIAT_RAIL
    bridge = SimulatedBridge(clock=Clock())
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    await an_engine(bridge).step(session, intent_id)

    assert (await bridge.status(f"sim_{intent_id}")).raw["destination_address"] == SETTLEMENT


async def test_a_fiat_rail_with_no_settlement_address_fails_rather_than_guesses(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    issuer.funding_model = FundingModel.FIAT_RAIL
    engine = TopUpEngine(SimulatedBridge(clock=Clock()), policy=TopUpPolicy())
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    outcome = await engine.step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE
    assert "settlement address" in outcome.detail
    assert (await reload_intent(session, intent_id)).last_error is not None


async def test_a_crypto_card_with_no_deposit_address_fails(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    issuer.deposit_address = None
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE


async def test_an_unavailable_bridge_is_retried_in_place(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # The state phase 5 had to add a self-loop for (§9.9): nothing was submitted,
    # so the intent has not moved, but the attempt has to be countable.
    bridge = SimulatedBridge(failure_mode=BridgeFailureMode.SUBMIT_UNAVAILABLE, clock=Clock())
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    outcome = await an_engine(bridge).step(session, intent_id)

    assert outcome.to_state is FundingState.DEPOSIT_CONFIRMED
    assert not outcome.progressed
    persisted = await reload_intent(session, intent_id)
    assert persisted.retry_count == 1
    assert (await ledger_for_intent(session, intent_id))[
        -1
    ].event_type == event_types.INTENT_RETRIED


async def test_a_refused_bridge_order_fails_the_intent_at_once(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # Nothing is in flight and asking again will not change the answer, so
    # spending five retries on it would only delay the FAILED an operator needs.
    bridge = SimulatedBridge(failure_mode=BridgeFailureMode.SUBMIT_REJECTED, clock=Clock())
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    outcome = await an_engine(bridge).step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE
    assert (await reload_intent(session, intent_id)).retry_count == 0


async def test_retries_stop_at_the_cap_and_the_intent_fails(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # SPEC.md §5.3: retry with backoff *up to a cap*, then FAILED_* with a reason.
    bridge = SimulatedBridge(failure_mode=BridgeFailureMode.SUBMIT_UNAVAILABLE, clock=Clock())
    engine = an_engine(bridge, max_retries=2)
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    first = await engine.step(session, intent_id)
    second = await engine.step(session, intent_id)
    third = await engine.step(session, intent_id)

    assert (first.to_state, second.to_state) == (
        FundingState.DEPOSIT_CONFIRMED,
        FundingState.DEPOSIT_CONFIRMED,
    )
    assert third.to_state is FundingState.FAILED_BRIDGE
    assert "gave up after 2 attempts" in third.detail
    # The reason survives on the intent, not only in the ledger.
    assert "gave up after 2 attempts" in str((await reload_intent(session, intent_id)).last_error)


# ------------------------------------------------------- BRIDGING -> ... -----


async def test_a_transfer_still_in_flight_retries_in_place(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=60, clock=clock)
    engine = an_engine(bridge)
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)
    await engine.step(session, intent_id)  # -> BRIDGING

    outcome = await engine.step(session, intent_id)

    assert outcome.to_state is FundingState.BRIDGING
    assert (await reload_intent(session, intent_id)).retry_count == 1


async def test_a_delivered_transfer_records_what_arrived(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # SPEC.md §11: bridged amounts arrive net of fees, and the fee has to remain
    # visible as the difference between two recorded numbers (§9.10).
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=30, fee_minor=150, clock=clock)
    engine = an_engine(bridge)
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED, amount_minor=2500)
    await engine.step(session, intent_id)
    clock.advance(30)

    outcome = await engine.step(session, intent_id)

    assert outcome.to_state is FundingState.BRIDGED
    persisted = await reload_intent(session, intent_id)
    assert persisted.amount_minor == 2500
    assert persisted.bridged_amount_minor == 2350
    context = (await ledger_for_intent(session, intent_id))[-1].payload["context"]
    assert context["fee_minor"] == 150
    assert context["destination_tx_ref"]


async def test_a_failed_transfer_fails_the_intent_with_the_bridges_reason(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    clock = Clock()
    bridge = SimulatedBridge(
        latency_seconds=30, failure_mode=BridgeFailureMode.TRANSFER_FAILED, clock=clock
    )
    engine = an_engine(bridge)
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)
    await engine.step(session, intent_id)
    clock.advance(30)

    outcome = await engine.step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE
    assert "simulated destination-chain failure" in outcome.detail


async def test_a_bridge_reference_the_provider_does_not_know_fails_the_intent(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # `UnknownTransferError` is not retryable: it will not become known by
    # asking again, and spinning to the cap on it wastes the budget.
    intent_id = await an_intent(seed_intent, FundingState.BRIDGING)
    await advance(session, intent_id, FundingState.BRIDGING, updates={"bridge_ref": "sim_nothing"})

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE


async def test_bridging_with_no_reference_at_all_fails_rather_than_hangs(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # Unreachable through `step()`, and unrecoverable if it ever happens: there
    # is nothing to ask the bridge about.
    intent_id = await an_intent(seed_intent, FundingState.BRIDGING)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE
    assert "no bridge reference" in outcome.detail


async def test_a_delivery_in_another_currency_is_not_guessed_at(
    session: AsyncSession,
    seed_intent: SeedIntent,
    issuer: FakeIssuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=0, clock=clock)
    engine = an_engine(bridge)
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED, currency="USD")
    await engine.step(session, intent_id)

    original = bridge.status

    async def in_euros(bridge_ref: str) -> object:
        transfer = await original(bridge_ref)
        return transfer.model_copy(update={"amount_out": Money(2500, "EUR")})

    monkeypatch.setattr(bridge, "status", in_euros)
    outcome = await engine.step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_BRIDGE
    assert "usable amount" in outcome.detail


# -------------------------------------------------- BRIDGED / FUNDING -> ... --


async def test_a_bridged_intent_funds_the_card_and_lands_on_funded(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    intent_id = await an_intent(seed_intent, FundingState.BRIDGED)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.from_state is FundingState.BRIDGED
    assert outcome.to_state is FundingState.FUNDED
    persisted = await reload_intent(session, intent_id)
    assert persisted.issuer_funding_ref == "prov_fund_1"
    # FUNDING is entered before the call and is on the record, not skipped over.
    states = [event.state_after for event in await ledger_for_intent(session, intent_id)]
    assert states == [str(FundingState.FUNDING), str(FundingState.FUNDED)]


async def test_the_card_is_funded_with_what_the_bridge_delivered(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # Not with what was deposited. The difference is the bridge's fee, and
    # funding a card with it is funding a card with money nobody has.
    intent_id = await an_intent(seed_intent, FundingState.BRIDGING, amount_minor=2500)
    await advance(
        session,
        intent_id,
        FundingState.BRIDGED,
        updates={"bridged_amount_minor": 2350},
    )

    await an_engine().step(session, intent_id)

    assert issuer.funded[0][1] == Money(2350, "USD")


async def test_fund_card_is_called_with_the_intent_id_as_its_idempotency_key(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # SPEC.md §10's requirement on every adapter: same funding_ref twice funds
    # once. The engine's side of that bargain is to always send the same ref.
    issuer.status = FundingStatus.PENDING
    engine = an_engine()
    intent_id = await an_intent(seed_intent, FundingState.BRIDGED)

    await engine.step(session, intent_id)
    await engine.step(session, intent_id)

    assert [call[2] for call in issuer.funded] == [str(intent_id), str(intent_id)]


async def test_pending_funding_is_a_normal_answer_and_the_engine_waits(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # A CRYPTO_DEPOSIT provider asked to fund before its own deposit confirms has
    # nothing to report yet (§7.1). Treating that as a failure would fail every
    # top-up at that kind of provider.
    issuer.status = FundingStatus.PENDING
    intent_id = await an_intent(seed_intent, FundingState.FUNDING)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.FUNDING
    persisted = await reload_intent(session, intent_id)
    assert persisted.retry_count == 1
    assert persisted.state is FundingState.FUNDING


async def test_a_refused_funding_fails_the_intent(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    issuer.status = FundingStatus.FAILED
    intent_id = await an_intent(seed_intent, FundingState.FUNDING)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.FAILED_FUNDING


async def test_a_busy_provider_is_retried_and_a_refusing_one_is_not(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # The whole point of the marker phase 5a added: same exception type, two
    # different answers, and the engine never learns which provider raised.
    issuer.raises = IssuerError("provider is busy", retryable=True)
    busy_intent = await an_intent(seed_intent, FundingState.FUNDING)
    refused_intent = await an_intent(seed_intent, FundingState.FUNDING, card_id="card_2")

    busy = await an_engine().step(session, busy_intent)
    issuer.raises = IssuerError("no such card")
    refused = await an_engine().step(session, refused_intent)

    assert busy.to_state is FundingState.FUNDING
    assert refused.to_state is FundingState.FAILED_FUNDING


async def test_an_unexpected_exception_leaves_the_intent_exactly_where_it_was(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # The third case (§9.1). A bug in our own code is not evidence that a funding
    # failed, and FAILED_* is terminal — so the engine does not decide anything.
    # The reconciler will step it again once the deploy is fixed.
    issuer.raises = AttributeError("a bad deploy")
    intent_id = await an_intent(seed_intent, FundingState.FUNDING)

    with pytest.raises(AttributeError):
        await an_engine().step(session, intent_id)

    persisted = await reload_intent(session, intent_id)
    assert persisted.state is FundingState.FUNDING
    assert persisted.retry_count == 0
    assert await ledger_for_intent(session, intent_id) == []


async def test_a_provider_that_cannot_be_read_at_all_is_not_a_funding_failure(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    issuer.get_card_raises = IssuerError("provider is busy", retryable=True)
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is FundingState.DEPOSIT_CONFIRMED
    assert (await reload_intent(session, intent_id)).retry_count == 1


# --------------------------------------------------- states it leaves alone --


@pytest.mark.parametrize(
    ("state", "why"),
    [
        (FundingState.PENDING, "watcher"),
        (FundingState.FUNDED, "settlement"),
        (FundingState.SETTLED, "terminal"),
        (FundingState.FAILED_BRIDGE, "terminal"),
    ],
)
async def test_the_engine_does_not_touch_states_that_are_not_its(
    session: AsyncSession,
    seed_intent: SeedIntent,
    issuer: FakeIssuer,
    state: FundingState,
    why: str,
) -> None:
    # PENDING is the watcher's (only a confirmed deposit may move it) and FUNDED
    # is the settlement consumer's (only the provider can say it settled).
    intent_id = await an_intent(seed_intent, state)

    outcome = await an_engine().step(session, intent_id)

    assert outcome.to_state is state
    assert not outcome.progressed
    assert why in outcome.detail
    assert await ledger_for_intent(session, intent_id) == []
    assert issuer.funded == []


# ------------------------------------------------------------ end to end ----


async def test_a_deposit_walks_all_the_way_to_funded_with_every_hop_ledgered(
    session: AsyncSession, seed_intent: SeedIntent, issuer: FakeIssuer
) -> None:
    # The demo target, minus the chain: an intent that starts at a confirmed
    # deposit reaches FUNDED, and the ledger is a complete account of how.
    clock = Clock()
    engine = an_engine(SimulatedBridge(latency_seconds=30, fee_minor=50, clock=clock))
    intent_id = await an_intent(seed_intent, FundingState.DEPOSIT_CONFIRMED, amount_minor=2500)

    outcomes: list[StepOutcome] = [await engine.step(session, intent_id)]
    clock.advance(30)
    while outcomes[-1].to_state not in (FundingState.FUNDED, FundingState.FAILED_FUNDING):
        outcomes.append(await engine.step(session, intent_id))

    assert outcomes[-1].to_state is FundingState.FUNDED
    states = [event.state_after for event in await ledger_for_intent(session, intent_id)]
    assert states == [
        str(FundingState.BRIDGING),
        str(FundingState.BRIDGED),
        str(FundingState.FUNDING),
        str(FundingState.FUNDED),
    ]
    assert issuer.funded[0][1] == Money(2450, "USD")  # 2500 deposited, 50 in fees
