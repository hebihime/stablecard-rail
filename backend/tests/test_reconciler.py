"""The reconciler (SPEC.md §5.3), with failures injected by the simulator.

This is the part that runs when nothing happens, so the tests are mostly about
time: an intent is stuck, then it is out of backoff, then it is out of retries.
Every clock here is one the test moves by hand, because a reconciler tested with
`sleep` is a reconciler nobody runs the slow paths of.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.bridge.simulated import BridgeFailureMode, SimulatedBridge
from app.core.config import Settings
from app.core.money import Money
from app.funding.engine import TopUpEngine, TopUpPolicy
from app.funding.reconciler import (
    RECONCILABLE_STATES,
    ReconcilerPolicy,
    due_after,
    find_stuck,
    reconcile,
)
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
)
from tests.support import SeedIntent, reload_intent

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
SAFE = "0xSafe000000000000000000000000000000000001"


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class StubIssuer(CardIssuerAdapter):
    """A crypto-deposit provider that funds whatever it is asked to."""

    provider_id = "stub_reconcile"
    funding_model = FundingModel.CRYPTO_DEPOSIT

    def __init__(self) -> None:
        self.status = FundingStatus.SUCCEEDED

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        raise NotImplementedError

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        raise NotImplementedError

    async def get_card(self, card_id: str) -> Card:
        return Card(
            provider_id=self.provider_id,
            card_id=card_id,
            cardholder_id="ch_1",
            state=CardState.ACTIVE,
            last_four="4242",
            exp_month=12,
            exp_year=2030,
            currency="USD",
            deposit_address=SAFE,
            created_at=NOW,
        )

    async def activate_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def freeze_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def cancel_card(self, card_id: str) -> Card:
        raise NotImplementedError

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        return FundingResult(
            provider_id=self.provider_id,
            card_id=card_id,
            funding_ref=funding_ref,
            issuer_funding_ref="stub_fund_1",
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
def issuer() -> StubIssuer:
    adapter = StubIssuer()
    registry.register(adapter.provider_id, lambda: adapter, replace=True)
    return adapter


def an_engine(bridge: SimulatedBridge) -> TopUpEngine:
    return TopUpEngine(bridge, policy=TopUpPolicy(max_retries=2, settlement_address="0xTreasury"))


async def a_stuck_intent(
    session: AsyncSession,
    seed_intent: SeedIntent,
    state: FundingState,
    *,
    stuck_for: int = 600,
    **overrides: object,
) -> uuid.UUID:
    """An intent whose state has not changed for `stuck_for` seconds."""
    intent = await seed_intent(state=state, provider_id="stub_reconcile", **overrides)  # type: ignore[arg-type]
    intent.state_changed_at = NOW - timedelta(seconds=stuck_for)
    await session.commit()
    return intent.id


# ------------------------------------------------------------- the scan ------


async def test_the_scan_finds_only_states_it_can_recover(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    for state in FundingState:
        await a_stuck_intent(session, seed_intent, state, card_id=f"card_{state}")

    found = await find_stuck(session, now=NOW, policy=ReconcilerPolicy())

    assert {intent.state for intent in found} == set(RECONCILABLE_STATES)


async def test_funded_intents_are_never_reconciled(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # FUNDED waits for a settlement that at these providers may never be
    # attributable (§9.12). Failing an intent whose card *has the money* would
    # turn a provider limitation into a fake incident.
    await a_stuck_intent(session, seed_intent, FundingState.FUNDED, stuck_for=86_400)

    assert await find_stuck(session, now=NOW, policy=ReconcilerPolicy()) == []


async def test_a_recently_changed_intent_is_not_stuck(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    await a_stuck_intent(session, seed_intent, FundingState.BRIDGING, stuck_for=30)

    assert (
        await find_stuck(session, now=NOW, policy=ReconcilerPolicy(stuck_after_seconds=120)) == []
    )


async def test_the_oldest_stuck_intent_is_examined_first(
    session: AsyncSession, seed_intent: SeedIntent
) -> None:
    # With a batch limit, ordering is what stops the longest-stuck intent from
    # being starved by newer ones on every pass.
    newer = await a_stuck_intent(
        session, seed_intent, FundingState.BRIDGING, stuck_for=200, card_id="card_new"
    )
    older = await a_stuck_intent(
        session, seed_intent, FundingState.BRIDGING, stuck_for=9_000, card_id="card_old"
    )

    found = await find_stuck(session, now=NOW, policy=ReconcilerPolicy(batch_limit=1))

    assert [intent.id for intent in found] == [older]
    assert newer not in [intent.id for intent in found]


async def test_the_batch_limit_is_honoured(session: AsyncSession, seed_intent: SeedIntent) -> None:
    for index in range(5):
        await a_stuck_intent(session, seed_intent, FundingState.BRIDGING, card_id=f"card_{index}")

    found = await find_stuck(session, now=NOW, policy=ReconcilerPolicy(batch_limit=3))

    assert len(found) == 3


# ------------------------------------------------------------- backoff -------


def test_backoff_doubles_with_every_retry() -> None:
    policy = ReconcilerPolicy(stuck_after_seconds=60, max_backoff_seconds=3_600)

    class Row:
        state_changed_at = NOW

        def __init__(self, retry_count: int) -> None:
            self.retry_count = retry_count

    assert due_after(Row(0), policy) == NOW + timedelta(seconds=60)  # type: ignore[arg-type]
    assert due_after(Row(1), policy) == NOW + timedelta(seconds=120)  # type: ignore[arg-type]
    assert due_after(Row(4), policy) == NOW + timedelta(seconds=960)  # type: ignore[arg-type]
    # Capped, so a much-retried intent is still looked at eventually.
    assert due_after(Row(20), policy) == NOW + timedelta(seconds=3_600)  # type: ignore[arg-type]


async def test_an_intent_inside_its_backoff_window_is_left_waiting(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # Stuck for 200s with two retries behind it: the window is 60 * 2**2 = 240s,
    # so it is not eligible yet. "Waiting" and "idle" are different, and the
    # report says which.
    intent_id = await a_stuck_intent(
        session, seed_intent, FundingState.BRIDGING, stuck_for=200, retry_count=2
    )
    engine = an_engine(SimulatedBridge(clock=Clock()))

    report = await reconcile(
        session,
        engine,
        now=NOW,
        policy=ReconcilerPolicy(stuck_after_seconds=60),
    )

    assert report.stepped == ()
    assert [plan.intent_id for plan in report.waiting] == [intent_id]
    assert report.waiting[0].due_at == NOW - timedelta(seconds=200) + timedelta(seconds=240)
    assert report.examined == 1


async def test_the_same_intent_is_stepped_once_the_window_passes(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    intent_id = await a_stuck_intent(
        session, seed_intent, FundingState.BRIDGING, stuck_for=200, retry_count=2
    )
    engine = an_engine(SimulatedBridge(clock=Clock()))
    policy = ReconcilerPolicy(stuck_after_seconds=60)

    await reconcile(session, engine, now=NOW, policy=policy)
    later = await reconcile(session, engine, now=NOW + timedelta(seconds=60), policy=policy)

    assert [outcome.intent_id for outcome in later.stepped] == [intent_id]


# -------------------------------------------------- recovering real stalls ---


async def test_a_stuck_bridge_is_retried_then_failed_at_the_cap(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # The simulator's `stuck` mode is the reason it exists (§9.4): a bridge that
    # accepted the order and went silent. Nothing but elapsed time distinguishes
    # it from a slow one, which is the whole argument for this component.
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=30, failure_mode=BridgeFailureMode.STUCK, clock=clock)
    engine = an_engine(bridge)
    intent_id = await a_stuck_intent(session, seed_intent, FundingState.DEPOSIT_CONFIRMED)
    policy = ReconcilerPolicy(stuck_after_seconds=60, max_backoff_seconds=600)

    # Submit succeeds, so the intent reaches BRIDGING and then stops moving.
    first = await reconcile(session, engine, now=NOW, policy=policy)
    assert first.stepped[0].to_state is FundingState.BRIDGING

    states = []
    at = NOW
    for _ in range(4):
        at += timedelta(seconds=1_200)
        clock.now = at
        report = await reconcile(session, engine, now=at, policy=policy)
        if report.stepped:
            states.append(report.stepped[0].to_state)

    # Two retries in place (the cap), then failed with the reason recorded.
    assert states == [
        FundingState.BRIDGING,
        FundingState.BRIDGING,
        FundingState.FAILED_BRIDGE,
    ]
    persisted = await reload_intent(session, intent_id)
    assert persisted.state is FundingState.FAILED_BRIDGE
    assert persisted.retry_count == 2
    assert "gave up after 2 attempts" in str(persisted.last_error)


async def test_a_delivered_bridge_is_carried_the_rest_of_the_way(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # The recovery that matters: an intent abandoned mid-flight reaches FUNDED
    # with no help from the watcher and no HTTP request.
    clock = Clock()
    engine = an_engine(SimulatedBridge(latency_seconds=1, clock=clock))
    intent_id = await a_stuck_intent(session, seed_intent, FundingState.DEPOSIT_CONFIRMED)
    policy = ReconcilerPolicy(stuck_after_seconds=1)

    at = NOW
    for _ in range(4):
        at += timedelta(seconds=600)
        clock.now = at
        await reconcile(session, engine, now=at, policy=policy)

    assert (await reload_intent(session, intent_id)).state is FundingState.FUNDED


async def test_an_intent_stalled_before_its_first_transition_is_recovered(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # The crash window between `create_intent()` and the confirm: the deposit
    # already *has* an intent, so every later watcher poll records it as a
    # duplicate and moves on. Nothing but this would ever close it (§9.13).
    intent_id = await a_stuck_intent(
        session, seed_intent, FundingState.PENDING, deposit_tx_ref="sig-stalled"
    )
    engine = an_engine(SimulatedBridge(clock=Clock()))

    report = await reconcile(session, engine, now=NOW, policy=ReconcilerPolicy())

    assert report.stepped[0].to_state is FundingState.DEPOSIT_CONFIRMED
    assert (await reload_intent(session, intent_id)).state is FundingState.DEPOSIT_CONFIRMED


async def test_an_intent_awaiting_a_deposit_is_not_stuck(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # PENDING with no deposit reference means nobody has sent anything. There is
    # nothing stuck here; there is an invoice waiting to be paid.
    intent_id = await a_stuck_intent(session, seed_intent, FundingState.PENDING, stuck_for=86_400)
    engine = an_engine(SimulatedBridge(clock=Clock()))

    report = await reconcile(session, engine, now=NOW, policy=ReconcilerPolicy())

    assert report.stepped == ()
    assert (await reload_intent(session, intent_id)).state is FundingState.PENDING


async def test_a_pass_over_nothing_reports_nothing(
    session: AsyncSession, issuer: StubIssuer
) -> None:
    report = await reconcile(
        session, an_engine(SimulatedBridge(clock=Clock())), now=NOW, policy=ReconcilerPolicy()
    )

    assert report == type(report)()
    assert report.examined == 0


async def test_a_pass_with_no_policy_uses_the_defaults(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    await a_stuck_intent(session, seed_intent, FundingState.BRIDGED, stuck_for=600)

    report = await reconcile(session, an_engine(SimulatedBridge(clock=Clock())), now=NOW)

    assert len(report.stepped) == 1


# -------------------------------------------------------------- settings -----


def test_every_threshold_comes_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # SPEC.md §5.3: "All thresholds config-driven."
    monkeypatch.setenv("RECONCILER_STUCK_AFTER_SECONDS", "45")
    monkeypatch.setenv("RECONCILER_MAX_BACKOFF_SECONDS", "900")
    monkeypatch.setenv("RECONCILER_BATCH_LIMIT", "10")

    policy = ReconcilerPolicy.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]

    assert policy == ReconcilerPolicy(
        stuck_after_seconds=45, max_backoff_seconds=900, batch_limit=10
    )


def test_the_engines_policy_comes_from_configuration_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUNDING_MAX_RETRIES", "9")
    monkeypatch.setenv("FUNDING_SOURCE_CHAIN", "solana-mainnet")
    monkeypatch.setenv("FUNDING_DESTINATION_CHAIN", "gnosis")
    monkeypatch.setenv("FUNDING_SETTLEMENT_ADDRESS", "0xTreasury")

    policy = TopUpPolicy.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]

    assert policy == TopUpPolicy(
        max_retries=9,
        source_chain="solana-mainnet",
        destination_chain="gnosis",
        settlement_address="0xTreasury",
    )
