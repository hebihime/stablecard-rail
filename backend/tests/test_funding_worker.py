"""The worker pass: poll, drive, reconcile (SPEC.md §5.2, §5.3).

The thing being pinned here is the division of labour. New work is driven
promptly; work that has already failed once is paced by the reconciler's backoff;
and no intent is stepped twice in one pass, because a double step would poll the
same bridge twice and count the second answer as a retry.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.rpc import SolanaRpcClient
from app.chain.solana_watcher import SOLANA_DEVNET, SolanaDepositWatcher
from app.core.config import Settings
from app.core.money import Money
from app.funding.engine import TopUpEngine, TopUpPolicy
from app.funding.reconciler import ReconcilerPolicy
from app.funding.routes import register_route, watched_addresses
from app.funding.states import FundingState
from app.funding.worker import ENGINE_STATES, WorkerPolicy, find_ready, run_pass
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

FIXTURES = Path(__file__).parent / "fixtures" / "solana"
RPC_URL = "https://api.devnet.solana.com"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
SAFE = "0xSafe000000000000000000000000000000000001"
DEPOSIT_ACCOUNT = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"
DEPOSIT_SIGNATURE = (
    "5Fig5NhWgYLW9eTMBqQuQiUW9uMzySFSkmK6zjjW5yqydgKLQjxSJAge8k5bfo29VCyiGkgptSBXdF2JY5H9ZYRw"
)
USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class StubIssuer(CardIssuerAdapter):
    provider_id = "stub_worker"
    funding_model = FundingModel.CRYPTO_DEPOSIT

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
            status=FundingStatus.SUCCEEDED,
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


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting."""


def an_engine(**overrides: object) -> TopUpEngine:
    from app.chain.bridge.simulated import SimulatedBridge

    return TopUpEngine(
        SimulatedBridge(clock=Clock(), **overrides),  # type: ignore[arg-type]
        policy=TopUpPolicy(max_retries=2, settlement_address="0xTreasury"),
    )


async def an_intent(
    session: AsyncSession,
    seed_intent: SeedIntent,
    state: FundingState,
    *,
    age_seconds: int = 60,
    **overrides: object,
) -> uuid.UUID:
    intent = await seed_intent(state=state, provider_id="stub_worker", **overrides)  # type: ignore[arg-type]
    intent.state_changed_at = NOW - timedelta(seconds=age_seconds)
    await session.commit()
    return intent.id


def a_watcher() -> SolanaDepositWatcher:
    return SolanaDepositWatcher(
        SolanaRpcClient(rpc_url=RPC_URL, sleep=_no_sleep),
        deposit_address=DEPOSIT_ACCOUNT,
        mint=USDC_DEVNET_MINT,
    )


def rpc_serves_the_recorded_deposit() -> respx.Route:
    entry = next(
        candidate
        for candidate in fixture("signatures_for_deposit_account")["result"]
        if candidate["signature"] == DEPOSIT_SIGNATURE
    )

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [entry]})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": fixture("transaction_transfer_checked")["result"],
            },
        )

    return respx.post(RPC_URL).mock(side_effect=responder)


# ------------------------------------------------------- what gets driven ----


async def test_a_fresh_intent_is_driven_promptly(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # The reason the worker does not simply call the reconciler: its threshold is
    # minutes, and a confirmed deposit should reach the bridge sooner than that.
    intent_id = await an_intent(
        session, seed_intent, FundingState.DEPOSIT_CONFIRMED, age_seconds=10
    )

    report = await run_pass(session, an_engine(), watchers=[], now=NOW)

    assert [outcome.intent_id for outcome in report.driven] == [intent_id]
    assert [outcome.to_state for outcome in report.driven] == [FundingState.BRIDGING]
    assert report.reconciled.stepped == ()
    assert report.stepped == 1


async def test_an_intent_younger_than_the_first_attempt_delay_is_left_alone(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # Whichever process created it is probably about to step it; racing that just
    # contends on the row lock.
    await an_intent(session, seed_intent, FundingState.DEPOSIT_CONFIRMED, age_seconds=1)

    report = await run_pass(
        session,
        an_engine(),
        watchers=[],
        now=NOW,
        policy=WorkerPolicy(first_attempt_after_seconds=3),
    )

    assert report.driven == ()


async def test_an_intent_that_has_already_retried_is_left_to_the_reconciler(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # This is the important one. Driving a retried intent on every pass would
    # spend the whole retry budget as fast as the loop runs, and a bridge with
    # thirty seconds of latency would be failed in five.
    intent_id = await an_intent(
        session, seed_intent, FundingState.BRIDGING, age_seconds=10, retry_count=1
    )

    ready = await find_ready(session, now=NOW, policy=WorkerPolicy())

    assert [intent.id for intent in ready] == []
    assert intent_id not in [intent.id for intent in ready]


@pytest.mark.parametrize("state", sorted(set(FundingState) - set(ENGINE_STATES)))
async def test_states_the_engine_does_not_own_are_never_driven(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer, state: FundingState
) -> None:
    # PENDING is the watcher's and FUNDED is the settlement consumer's; the rest
    # are terminal. Only the reconciler may touch a stalled PENDING (§9.13).
    await an_intent(session, seed_intent, state, age_seconds=600)

    assert await find_ready(session, now=NOW, policy=WorkerPolicy()) == []


async def test_the_batch_limit_bounds_one_pass(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    for index in range(4):
        await an_intent(
            session, seed_intent, FundingState.DEPOSIT_CONFIRMED, card_id=f"card_{index}"
        )

    ready = await find_ready(session, now=NOW, policy=WorkerPolicy(batch_limit=2))

    assert len(ready) == 2


# ------------------------------------------------- driver and reconciler ----


async def test_one_pass_never_steps_the_same_intent_twice(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    # An un-retried intent that is *also* old enough to look stuck appears to
    # both halves. Stepping it twice would poll the bridge twice and count the
    # second answer as a retry — so the driver tells the reconciler what it took.
    intent_id = await an_intent(
        session, seed_intent, FundingState.DEPOSIT_CONFIRMED, age_seconds=9_000
    )

    report = await run_pass(
        session,
        an_engine(),
        watchers=[],
        now=NOW,
        reconciler_policy=ReconcilerPolicy(stuck_after_seconds=60),
    )

    assert [outcome.intent_id for outcome in report.driven] == [intent_id]
    assert report.reconciled.stepped == ()
    assert (await reload_intent(session, intent_id)).retry_count == 0


async def test_a_stuck_retried_intent_is_reconciled_in_the_same_pass(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    fresh = await an_intent(
        session, seed_intent, FundingState.DEPOSIT_CONFIRMED, age_seconds=30, card_id="card_fresh"
    )
    stuck = await an_intent(
        session,
        seed_intent,
        FundingState.BRIDGED,
        age_seconds=9_000,
        retry_count=1,
        card_id="card_stuck",
    )

    report = await run_pass(
        session,
        an_engine(),
        watchers=[],
        now=NOW,
        reconciler_policy=ReconcilerPolicy(stuck_after_seconds=60, max_backoff_seconds=600),
    )

    assert [outcome.intent_id for outcome in report.driven] == [fresh]
    assert [outcome.intent_id for outcome in report.reconciled.stepped] == [stuck]


# --------------------------------------------------------- with a watcher ----


@respx.mock
async def test_a_pass_polls_the_chain_and_drives_what_it_finds(
    session: AsyncSession, issuer: StubIssuer
) -> None:
    # The whole point of the worker in one test: nothing calls anything, and a
    # deposit still becomes a bridge order.
    rpc_serves_the_recorded_deposit()
    await register_route(
        session,
        chain=SOLANA_DEVNET,
        deposit_address=DEPOSIT_ACCOUNT,
        provider_id="stub_worker",
        card_id="card_1",
    )
    await session.commit()

    # `now` is ahead of the deposit's own confirmation, so the intent it opens is
    # already older than the first-attempt delay.
    report = await run_pass(
        session,
        an_engine(),
        watchers=[a_watcher()],
        now=datetime.now(UTC) + timedelta(seconds=30),
    )

    assert report.opened == 1
    assert [outcome.to_state for outcome in report.driven] == [FundingState.BRIDGING]


@respx.mock
async def test_a_pass_with_nothing_to_do_reports_nothing(
    session: AsyncSession, issuer: StubIssuer
) -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("signatures_none")))

    report = await run_pass(session, an_engine(), watchers=[a_watcher()], now=NOW)

    assert report.opened == 0
    assert report.stepped == 0
    assert report.did_anything is False


# ----------------------------------------------- what the worker watches ----


async def test_the_routes_table_is_the_list_of_addresses_to_watch(
    session: AsyncSession,
) -> None:
    # A route exists because somebody was told to send money to that address, so
    # everything registered has to be polled and nothing else needs to be.
    await register_route(
        session,
        chain=SOLANA_DEVNET,
        deposit_address=DEPOSIT_ACCOUNT,
        provider_id="stub_worker",
        card_id="card_1",
    )
    await register_route(
        session,
        chain=SOLANA_DEVNET,
        deposit_address="6Be1VPtVP9tcx9JN8HcPXcndEriVQofUGAwLTFZLuWxG",
        provider_id="stub_worker",
        card_id="card_2",
    )
    await register_route(
        session,
        chain="gnosis-chiado",
        deposit_address="0xSomewhereElse",
        provider_id="stub_worker",
        card_id="card_3",
    )
    await session.commit()

    assert await watched_addresses(session, chain=SOLANA_DEVNET) == [
        "6Be1VPtVP9tcx9JN8HcPXcndEriVQofUGAwLTFZLuWxG",
        DEPOSIT_ACCOUNT,
    ]
    assert await watched_addresses(session, chain="gnosis-chiado") == ["0xSomewhereElse"]


async def test_an_unwatched_chain_has_nothing_to_poll(session: AsyncSession) -> None:
    assert await watched_addresses(session, chain=SOLANA_DEVNET) == []


def test_the_worker_reads_its_pacing_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_FIRST_ATTEMPT_AFTER_SECONDS", "0.5")
    monkeypatch.setenv("RECONCILER_BATCH_LIMIT", "7")

    policy = WorkerPolicy.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]

    assert policy == WorkerPolicy(first_attempt_after_seconds=0.5, batch_limit=7)


async def test_a_pass_carries_the_intents_it_touched(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    from app.funding.worker import intent_ids

    driven = await an_intent(session, seed_intent, FundingState.BRIDGED, age_seconds=30)

    report = await run_pass(session, an_engine(), watchers=[], now=NOW)

    assert intent_ids(report) == (driven,)


async def test_the_reconciler_can_be_told_to_skip_an_intent(
    session: AsyncSession, seed_intent: SeedIntent, issuer: StubIssuer
) -> None:
    from app.funding.reconciler import reconcile

    intent_id = await an_intent(session, seed_intent, FundingState.BRIDGED, age_seconds=9_000)

    report = await reconcile(
        session,
        an_engine(),
        now=NOW,
        policy=ReconcilerPolicy(stuck_after_seconds=60),
        skip={intent_id},
    )

    assert report.stepped == ()
    assert report.waiting == ()
    assert (await reload_intent(session, intent_id)).state is FundingState.BRIDGED
