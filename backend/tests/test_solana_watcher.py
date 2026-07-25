"""The deposit watcher, against recorded devnet responses (SPEC.md §5.2 step 1).

The transfer these replay is real: 1.000000 USDC into
`GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU` on devnet, recorded by
`scripts/record_solana_fixtures.py`. One USDC is 1_000_000 base units and 100
cents, and the gap between those two numbers is most of what this file is about.

The suite never calls the RPC (SPEC.md §10).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.chain.rpc import SolanaRpcClient
from app.chain.solana_watcher import ConfirmedDeposit, IgnoredTransfer, SolanaDepositWatcher
from app.core.money import Money

FIXTURES = Path(__file__).parent / "fixtures" / "solana"
RPC_URL = "https://api.devnet.solana.com"

USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
DEPOSIT_ACCOUNT = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"
DEPOSIT_OWNER = "D7d6ZP2KcmHzhEh4nGrpe7zMKFjXP8wtvceecGB3fKVp"
DEPOSIT_SIGNATURE = (
    "5Fig5NhWgYLW9eTMBqQuQiUW9uMzySFSkmK6zjjW5yqydgKLQjxSJAge8k5bfo29VCyiGkgptSBXdF2JY5H9ZYRw"
)
DEPOSIT_SLOT = 478901561


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def recorded_signature_entry(signature: str = DEPOSIT_SIGNATURE) -> dict[str, Any]:
    """The recorded list entry for one signature — a real one, not a hand-made one."""
    for entry in fixture("signatures_for_deposit_account")["result"]:
        if entry["signature"] == signature:
            return dict(entry)
    raise AssertionError(f"{signature} is not in the recorded signature list")


def rpc_returns(
    signatures: list[dict[str, Any]],
    transactions: dict[str, Any] | None = None,
) -> respx.Route:
    """Answer both RPC methods from in-memory maps, by request body."""
    lookup = transactions or {}

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": signatures})
        signature = body["params"][0]
        if signature not in lookup:
            return httpx.Response(200, json=fixture("transaction_not_found"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": lookup[signature]})

    return respx.post(RPC_URL).mock(side_effect=responder)


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting."""


def watcher(**kwargs: Any) -> SolanaDepositWatcher:
    return SolanaDepositWatcher(
        SolanaRpcClient(rpc_url=RPC_URL, sleep=_no_sleep),
        deposit_address=kwargs.pop("deposit_address", DEPOSIT_ACCOUNT),
        mint=kwargs.pop("mint", USDC_DEVNET_MINT),
        **kwargs,
    )


def transfer_transaction() -> Any:
    return fixture("transaction_transfer_checked")["result"]


# ------------------------------------------------------------ the deposit ----


@respx.mock
async def test_a_real_devnet_transfer_becomes_one_deposit() -> None:
    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transfer_transaction()})

    page = await watcher().poll()

    assert len(page.deposits) == 1
    deposit = page.deposits[0]
    assert deposit.signature == DEPOSIT_SIGNATURE
    assert deposit.slot == DEPOSIT_SLOT
    assert deposit.deposit_address == DEPOSIT_ACCOUNT
    assert deposit.owner == DEPOSIT_OWNER
    assert deposit.mint == USDC_DEVNET_MINT


@respx.mock
async def test_one_usdc_is_a_hundred_cents_and_no_dust() -> None:
    # 1.000000 USDC = 1_000_000 base units at six decimals = 100 minor units of a
    # two-decimal currency. The conversion is the point: `Money` is cents, the
    # chain is base units, and nothing in between is a float.
    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transfer_transaction()})

    deposit = (await watcher().poll()).deposits[0]

    assert deposit.base_units == 1_000_000
    assert deposit.amount == Money(100, "USD")
    assert deposit.dust_base_units == 0
    assert isinstance(deposit.amount.amount_minor, int)


@respx.mock
async def test_the_chains_own_timestamp_is_kept_in_utc() -> None:
    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transfer_transaction()})

    deposit = (await watcher().poll()).deposits[0]

    assert deposit.block_time == datetime.fromtimestamp(1785021990, tz=UTC)
    assert deposit.block_time is not None and deposit.block_time.tzinfo is not None


@respx.mock
async def test_a_deposit_that_is_not_a_whole_number_of_cents_keeps_its_remainder() -> None:
    # Truncated, never rounded up: crediting a cent that did not arrive is how a
    # funding pipeline ends up short. What is left over is recorded rather than
    # silently dropped.
    transaction = transfer_transaction()
    for balance in transaction["meta"]["postTokenBalances"]:
        if balance["accountIndex"] == 2:
            balance["uiTokenAmount"]["amount"] = "4109234567"  # +234_567 base units

    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transaction})

    deposit = (await watcher().poll()).deposits[0]

    assert deposit.base_units == 234_567
    assert deposit.amount == Money(23, "USD")
    assert deposit.dust_base_units == 4_567


@respx.mock
async def test_a_first_deposit_into_a_brand_new_account_credits_the_whole_amount() -> None:
    # Derived fixture: an account created *by* the transfer has no
    # `preTokenBalances` entry at all. Reading a missing entry as anything but
    # zero makes every card's opening deposit either invisible or negative.
    rpc_returns(
        [recorded_signature_entry()],
        {DEPOSIT_SIGNATURE: fixture("transaction_first_deposit")["result"]},
    )

    deposit = (await watcher().poll()).deposits[0]

    assert deposit.base_units == 4_110_000_000
    assert deposit.amount == Money(411_000, "USD")


# ----------------------------------------------------- what is not a deposit --


def test_a_failed_transaction_really_does_carry_token_balances() -> None:
    # Evidence, from the recorded devnet failure, for the assumption the next
    # test depends on: `err` is set and `postTokenBalances` is populated anyway.
    # A watcher that reads balances without checking `err` has no idea.
    failed = fixture("transaction_failed")["result"]

    assert failed["meta"]["err"] == {"InstructionError": [0, {"Custom": 1}]}
    assert failed["meta"]["postTokenBalances"]


@respx.mock
async def test_a_failed_transaction_credits_nothing_even_though_it_has_balances() -> None:
    # The recorded failure moved nothing, so on its own it cannot show that `err`
    # is what stops the credit. This is the crediting transfer with that same
    # real `err` applied to it: +1 USDC in the balances, and still not a deposit.
    transaction = transfer_transaction()
    transaction["meta"]["err"] = fixture("transaction_failed")["result"]["meta"]["err"]
    entry = recorded_signature_entry()
    entry["err"] = None  # the index says fine; the transaction says otherwise
    rpc_returns([entry], {DEPOSIT_SIGNATURE: transaction})

    page = await watcher().poll()

    assert page.deposits == ()
    assert [ignored.reason for ignored in page.ignored] == ["transaction failed on chain"]


@respx.mock
async def test_a_signature_the_index_already_marks_failed_is_not_even_fetched() -> None:
    entry = recorded_signature_entry()
    entry["err"] = {"InstructionError": [0, {"Custom": 1}]}
    route = rpc_returns([entry], {DEPOSIT_SIGNATURE: transfer_transaction()})

    page = await watcher().poll()

    assert page.deposits == ()
    assert page.ignored[0].reason == "transaction failed on chain"
    assert route.call_count == 1  # the listing only; no getTransaction


@respx.mock
async def test_a_dust_deposit_opens_no_intent() -> None:
    # 0.000001 USDC is a real transfer of no creditable value. Opening an intent
    # for zero would put a `FundingIntent` with amount 0 into the machine, which
    # its own CHECK constraint forbids — better to refuse it here, with a reason.
    rpc_returns(
        [recorded_signature_entry()],
        {DEPOSIT_SIGNATURE: fixture("transaction_dust_deposit")["result"]},
    )

    page = await watcher().poll()

    assert page.deposits == ()
    assert page.ignored[0].reason == "below one minor unit of USD"
    assert page.ignored[0].base_units == 1


@respx.mock
async def test_money_leaving_the_account_is_not_a_deposit() -> None:
    # The same transaction seen from the *sender's* side: index 3 went down.
    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transfer_transaction()})

    page = await watcher(deposit_address="5iv62nJJJHsV7pgJcA3sf9kp98uWaQcjyKtxFZ5dEbcW").poll()

    assert page.deposits == ()
    assert page.ignored[0].reason == "not a credit to the watched account"
    assert page.ignored[0].base_units == -1_000_000


@respx.mock
async def test_a_transfer_of_some_other_token_is_ignored() -> None:
    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transfer_transaction()})

    page = await watcher(mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v").poll()

    assert page.deposits == ()
    assert page.ignored[0].reason == "no token balance for the watched mint"


@respx.mock
async def test_a_transaction_that_does_not_touch_the_account_is_ignored() -> None:
    rpc_returns([recorded_signature_entry()], {DEPOSIT_SIGNATURE: transfer_transaction()})

    page = await watcher(deposit_address="6Be1VPtVP9tcx9JN8HcPXcndEriVQofUGAwLTFZLuWxG").poll()

    assert page.deposits == ()
    assert page.ignored[0].reason == "watched account is not in this tx"


@respx.mock
async def test_a_transaction_with_no_block_time_still_yields_a_deposit() -> None:
    # `blockTime` is null for old slots. Inventing one would invent the ordering
    # with it, so the field is optional and the caller decides.
    transaction = transfer_transaction()
    transaction["blockTime"] = None
    entry = recorded_signature_entry()
    entry["blockTime"] = None
    rpc_returns([entry], {DEPOSIT_SIGNATURE: transaction})

    deposit = (await watcher().poll()).deposits[0]

    assert deposit.block_time is None


# ------------------------------------------------------ ordering and cursor --


@respx.mock
async def test_a_page_is_processed_oldest_first() -> None:
    # The node answers newest-first; the ledger has to record what the chain did,
    # in the order it did it.
    older = recorded_signature_entry()
    newer = dict(older, signature="newer-signature", slot=DEPOSIT_SLOT + 10)
    rpc_returns(
        [newer, older],  # as the node returns them
        {DEPOSIT_SIGNATURE: transfer_transaction(), "newer-signature": transfer_transaction()},
    )

    page = await watcher().poll()

    assert [deposit.signature for deposit in page.deposits] == [
        DEPOSIT_SIGNATURE,
        "newer-signature",
    ]
    assert page.cursor_signature == "newer-signature"
    assert page.cursor_slot == DEPOSIT_SLOT + 10


@respx.mock
async def test_the_cursor_advances_past_transfers_that_were_ignored() -> None:
    # An ignored transfer is *accounted for*. Leaving the cursor behind it would
    # re-examine the same dust deposit on every poll, forever.
    entry = recorded_signature_entry()
    rpc_returns([entry], {DEPOSIT_SIGNATURE: fixture("transaction_dust_deposit")["result"]})

    page = await watcher().poll()

    assert page.deposits == ()
    assert page.cursor_signature == DEPOSIT_SIGNATURE


@respx.mock
async def test_nothing_new_moves_no_cursor() -> None:
    rpc_returns([])

    page = await watcher().poll()

    assert page == type(page)()  # every field at its default
    assert page.cursor_signature is None


@respx.mock
async def test_a_transaction_that_is_not_readable_yet_stops_the_page() -> None:
    # This is the one that matters. A signature is in the index before the
    # transaction is fetchable, so skipping it would step the cursor over a
    # deposit that is seconds from appearing — and `until` means it would never
    # be asked for again.
    older = recorded_signature_entry()
    newer = dict(older, signature="not-readable-yet", slot=DEPOSIT_SLOT + 10)
    rpc_returns([newer, older], {DEPOSIT_SIGNATURE: transfer_transaction()})

    page = await watcher().poll()

    assert [deposit.signature for deposit in page.deposits] == [DEPOSIT_SIGNATURE]
    assert page.stopped_early is True
    # The cursor stops at the last one fully accounted for, so the unreadable
    # transaction is next poll's work rather than nobody's.
    assert page.cursor_signature == DEPOSIT_SIGNATURE


@respx.mock
async def test_the_cursor_is_handed_back_to_the_node_as_until() -> None:
    route = rpc_returns([])

    await watcher(page_limit=5).poll(until_signature="the-last-one-we-saw")

    options = json.loads(route.calls.last.request.content)["params"][1]
    assert options["until"] == "the-last-one-we-saw"
    assert options["limit"] == 5
    assert options["commitment"] == "finalized"


# -------------------------------------------------------------- the shape ----


def test_a_mint_with_fewer_decimals_than_the_currency_is_refused() -> None:
    with pytest.raises(ValueError, match="decimals"):
        watcher(decimals=1)


def test_the_watcher_reports_what_it_is_watching() -> None:
    subject = watcher()

    assert subject.deposit_address == DEPOSIT_ACCOUNT
    assert subject.chain == "solana-devnet"


def test_a_deposit_and_an_ignored_transfer_are_both_immutable() -> None:
    deposit = ConfirmedDeposit(
        chain="solana-devnet",
        deposit_address=DEPOSIT_ACCOUNT,
        mint=USDC_DEVNET_MINT,
        signature=DEPOSIT_SIGNATURE,
        slot=1,
        block_time=None,
        base_units=1_000_000,
        amount=Money(100, "USD"),
        dust_base_units=0,
    )
    ignored = IgnoredTransfer(DEPOSIT_SIGNATURE, 1, reason="because")

    with pytest.raises(AttributeError):
        deposit.slot = 2  # type: ignore[misc]
    with pytest.raises(AttributeError):
        ignored.reason = "changed"  # type: ignore[misc]
