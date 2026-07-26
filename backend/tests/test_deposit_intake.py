"""From a devnet transfer to a funding intent (SPEC.md §5.2 step 1).

The seam between the watcher and the state machine, and the place the pipeline's
at-least-once story is actually settled: intents first, cursor last, unique
`deposit_tx_ref` as the thing that makes a re-observation harmless.

Everything here replays the recorded devnet fixtures — the suite never calls the
RPC (SPEC.md §10).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.cursors import cursor_key, load_cursor
from app.chain.rpc import SolanaRpcClient
from app.chain.solana_watcher import SOLANA_DEVNET, SolanaDepositWatcher
from app.core.money import Money
from app.funding.deposits import collect_deposits, record_deposits
from app.funding.machine import get_intent
from app.funding.routes import register_route
from app.funding.states import FundingState
from app.ledger import event_types
from tests.support import all_ledger_events

FIXTURES = Path(__file__).parent / "fixtures" / "solana"
RPC_URL = "https://api.devnet.solana.com"

DEPOSIT_ACCOUNT = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"
DEPOSIT_SIGNATURE = (
    "5Fig5NhWgYLW9eTMBqQuQiUW9uMzySFSkmK6zjjW5yqydgKLQjxSJAge8k5bfo29VCyiGkgptSBXdF2JY5H9ZYRw"
)
DEPOSIT_SLOT = 478901561
USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def signature_entry(signature: str = DEPOSIT_SIGNATURE) -> dict[str, Any]:
    for entry in fixture("signatures_for_deposit_account")["result"]:
        if entry["signature"] == signature:
            return dict(entry)
    raise AssertionError(f"{signature} is not in the recorded signature list")


def rpc_returns(signatures: list[dict[str, Any]], transactions: dict[str, Any]) -> respx.Route:
    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": signatures})
        signature = body["params"][0]
        if signature not in transactions:
            return httpx.Response(200, json=fixture("transaction_not_found"))
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": transactions[signature]}
        )

    return respx.post(RPC_URL).mock(side_effect=responder)


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting."""


def a_watcher() -> SolanaDepositWatcher:
    return SolanaDepositWatcher(
        SolanaRpcClient(rpc_url=RPC_URL, sleep=_no_sleep),
        deposit_address=DEPOSIT_ACCOUNT,
        mint=USDC_DEVNET_MINT,
    )


async def a_route(session: AsyncSession) -> None:
    await register_route(
        session,
        chain=SOLANA_DEVNET,
        deposit_address=DEPOSIT_ACCOUNT,
        provider_id="gnosis_pay_mock",
        card_id="card_1",
    )
    await session.commit()


def transfer() -> Any:
    return fixture("transaction_transfer_checked")["result"]


# ------------------------------------------------------------ the happy path --


@respx.mock
async def test_a_devnet_transfer_opens_an_intent_at_deposit_confirmed(
    session: AsyncSession,
) -> None:
    # The first half of the demo target: a real USDC transfer becomes a funding
    # intent that has already cleared PENDING, because the watcher only reports
    # `finalized` deposits — there is nothing left to confirm.
    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})

    report = await collect_deposits(session, a_watcher())

    assert len(report.opened) == 1
    intent = await get_intent(session, report.opened[0])
    assert intent.state is FundingState.DEPOSIT_CONFIRMED
    assert intent.money == Money(100, "USD")  # 1.000000 USDC
    assert intent.deposit_tx_ref == DEPOSIT_SIGNATURE
    assert intent.provider_id == "gnosis_pay_mock"
    assert intent.card_id == "card_1"


@respx.mock
async def test_both_hops_are_ledgered_with_the_chain_context(session: AsyncSession) -> None:
    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})

    report = await collect_deposits(session, a_watcher())

    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == [
        event_types.INTENT_CREATED,
        event_types.INTENT_TRANSITIONED,
    ]
    created = events[0].payload
    assert created["deposit_tx_ref"] == DEPOSIT_SIGNATURE
    assert created["slot"] == DEPOSIT_SLOT
    assert created["base_units"] == 1_000_000
    assert created["mint"] == USDC_DEVNET_MINT
    assert str(report.opened[0]) == str(events[0].intent_id)


@respx.mock
async def test_the_cursor_moves_only_after_the_intents_exist(session: AsyncSession) -> None:
    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})

    await collect_deposits(session, a_watcher())

    cursor = await load_cursor(session, cursor_key(SOLANA_DEVNET, DEPOSIT_ACCOUNT))
    assert cursor is not None
    assert cursor.last_signature == DEPOSIT_SIGNATURE
    assert cursor.last_slot == DEPOSIT_SLOT


@respx.mock
async def test_the_next_poll_asks_only_for_what_is_new(session: AsyncSession) -> None:
    await a_route(session)
    route = rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})
    watcher = a_watcher()
    await collect_deposits(session, watcher)

    await collect_deposits(session, watcher)

    listings = [
        json.loads(call.request.content)
        for call in route.calls
        if json.loads(call.request.content)["method"] == "getSignaturesForAddress"
    ]
    assert "until" not in listings[0]["params"][1]
    assert listings[-1]["params"][1]["until"] == DEPOSIT_SIGNATURE


# ------------------------------------------------------ observed more than once --


@respx.mock
async def test_re_observing_a_deposit_does_not_open_a_second_intent(
    session: AsyncSession,
) -> None:
    # The crash window the ordering leaves open on purpose: intents committed,
    # cursor not. The unique index on `deposit_tx_ref` is what makes that safe,
    # so this drives the same page through twice with no cursor in between.
    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})
    watcher = a_watcher()

    first = await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)
    second = await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    assert len(first.opened) == 1
    assert second.opened == ()
    assert second.duplicates == (DEPOSIT_SIGNATURE,)
    intents = [
        event
        for event in await all_ledger_events(session)
        if event.event_type == event_types.INTENT_CREATED
    ]
    assert len(intents) == 1


@respx.mock
async def test_a_re_observed_deposit_is_ledgered_once_not_every_time(
    session: AsyncSession,
) -> None:
    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})
    watcher = a_watcher()
    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)
    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    duplicates = [
        event
        for event in await all_ledger_events(session)
        if event.event_type == event_types.DEPOSIT_DUPLICATE
    ]
    assert len(duplicates) == 1


# ------------------------------------------------- money with nowhere to go --


@respx.mock
async def test_a_deposit_with_no_route_is_recorded_and_not_credited(
    session: AsyncSession,
) -> None:
    # Real money at a real address, and no card claims it. Guessing would be
    # worse than stopping, and dropping it silently worse than either.
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})

    report = await collect_deposits(session, a_watcher())

    assert report.opened == ()
    assert report.unroutable == (DEPOSIT_SIGNATURE,)
    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == [event_types.DEPOSIT_UNROUTABLE]
    assert events[0].payload["deposit_address"] == DEPOSIT_ACCOUNT
    assert events[0].amount_minor == 100


@respx.mock
async def test_one_unroutable_deposit_does_not_stop_the_rest_of_the_page(
    session: AsyncSession,
) -> None:
    await a_route(session)
    routed = signature_entry()
    stray = dict(routed, signature="stray-signature", slot=DEPOSIT_SLOT + 5)
    stray_transaction = json.loads(json.dumps(transfer()))
    # Same shape, a different watched account: nothing routes it.
    rpc_returns(
        [stray, routed], {DEPOSIT_SIGNATURE: transfer(), "stray-signature": stray_transaction}
    )
    watcher = a_watcher()

    report = await record_deposits(session, await watcher.poll(), chain="unrouted-chain")

    assert report.opened == ()
    assert len(report.unroutable) == 2


@respx.mock
async def test_transfers_the_watcher_declined_are_ledgered_with_their_reason(
    session: AsyncSession,
) -> None:
    # Dust, failures and other people's tokens never become intents, but they
    # did touch the address — "nothing happened" and "we decided nothing
    # happened" are different (§9.7).
    await a_route(session)
    rpc_returns(
        [signature_entry()],
        {DEPOSIT_SIGNATURE: fixture("transaction_dust_deposit")["result"]},
    )

    report = await collect_deposits(session, a_watcher())

    assert report.opened == ()
    assert len(report.ignored) == 1
    events = await all_ledger_events(session)
    assert [event.event_type for event in events] == [event_types.TRANSFER_IGNORED]
    assert events[0].payload["reason"] == "below one minor unit of USD"
    assert events[0].payload["base_units"] == 1


@respx.mock
async def test_an_ignored_transfer_is_ledgered_once_however_often_it_is_seen(
    session: AsyncSession,
) -> None:
    await a_route(session)
    rpc_returns(
        [signature_entry()],
        {DEPOSIT_SIGNATURE: fixture("transaction_dust_deposit")["result"]},
    )
    watcher = a_watcher()

    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)
    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    assert len(await all_ledger_events(session)) == 1


@respx.mock
async def test_a_quiet_poll_changes_nothing(session: AsyncSession) -> None:
    await a_route(session)
    rpc_returns([], {})

    report = await collect_deposits(session, a_watcher())

    assert report.observed == 0
    assert await all_ledger_events(session) == []
    # No cursor either: there is nothing to have got to.
    assert await load_cursor(session, cursor_key(SOLANA_DEVNET, DEPOSIT_ACCOUNT)) is None


@respx.mock
async def test_an_integrity_error_that_is_not_a_duplicate_deposit_is_not_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The duplicate path catches `IntegrityError`, which is a wide net. It must
    # only cover the one constraint it is there for.
    from sqlalchemy.exc import IntegrityError

    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})

    async def _unrelated(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("some other constraint"))

    monkeypatch.setattr("app.funding.deposits.create_intent", _unrelated)

    with pytest.raises(IntegrityError):
        await collect_deposits(session, a_watcher())


@respx.mock
async def test_the_unique_index_is_the_backstop_when_the_precheck_misses(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two layers, the same shape as webhook dedup (§2.4): the question is the
    # ordinary path and the constraint is what makes it safe. Blinding the
    # question is how the race between two workers is reproduced deliberately —
    # both ask, both hear "no intent yet", and only one may be right.
    await a_route(session)
    rpc_returns([signature_entry()], {DEPOSIT_SIGNATURE: transfer()})
    watcher = a_watcher()
    first = await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)
    assert len(first.opened) == 1

    async def _blind(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.funding.deposits.intent_for_deposit", _blind)

    second = await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    assert second.opened == ()
    assert second.duplicates == (DEPOSIT_SIGNATURE,)
    created = [
        event
        for event in await all_ledger_events(session)
        if event.event_type == event_types.INTENT_CREATED
    ]
    assert len(created) == 1


@respx.mock
async def test_a_ledger_row_is_written_once_even_if_the_precheck_misses(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await a_route(session)
    rpc_returns(
        [signature_entry()],
        {DEPOSIT_SIGNATURE: fixture("transaction_dust_deposit")["result"]},
    )
    watcher = a_watcher()
    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    async def _blind(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.funding.deposits.find_by_idempotency_key", _blind)

    await record_deposits(session, await watcher.poll(), chain=SOLANA_DEVNET)

    ignored = [
        event
        for event in await all_ledger_events(session)
        if event.event_type == event_types.TRANSFER_IGNORED
    ]
    assert len(ignored) == 1
