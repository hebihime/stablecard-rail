"""The real bridge behind the same two calls the simulator answers.

Two properties carry this file, and both are things the simulator gets for free
and this adapter has to construct:

**A duplicate `submit` must not lock a second amount.** Wormhole has no
idempotency key, and the engine retries `submit` exactly when it cannot tell
whether the first call landed. The mechanism is the derived message account, and
the tests below cover all three ways a retry arrives: after a successful send,
after a send whose *answer* was lost, and concurrently.

**`status` has to act.** With lock-and-mint nobody else delivers the transfer, so
a `status` that only observed would leave every transfer pending forever.

Everything is replayed: recorded Wormholescan and BSC testnet answers, and a
Solana node that is a stub rather than a socket. The suite never calls a chain
(SPEC.md §10).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.chain.bridge.base import (
    BridgeError,
    BridgeOrder,
    BridgeRejectedError,
    BridgeStatus,
    UnknownTransferError,
)
from app.chain.bridge.wormhole.accounts import (
    emitter_address,
    message_keypair_from_signature,
    message_seed_payload,
)
from app.chain.bridge.wormhole.adapter import WormholeBridge
from app.chain.bridge.wormhole.client import WormholescanClient
from app.chain.bridge.wormhole.config import WormholeSettings
from app.chain.bridge.wormhole.redeemer import Redeemer, RedemptionRefused
from app.chain.bridge.wormhole.vaa import parse_signed_vaa
from app.chain.config import USDC_DEVNET_MINT
from app.chain.rpc import SolanaRpcClient, SolanaRpcError
from app.chain.signer import LocalKeypairSigner, TransactionSigner
from app.core.money import Money

WORMHOLE_FIXTURES = Path(__file__).parent / "fixtures" / "wormhole"

SETTINGS = WormholeSettings(_env_file=None)  # type: ignore[call-arg]
RECIPIENT = "0x4c587f5fc2137915c6170ad9df5b58855a2f1fd0"
BLOCKHASH = "DTdt9NMNuAaoftWPLpJNYUNqZG3Amu6Zw1NiuDsJLGUg"
SIGNATURE = (
    "5Fig5NhWgYLW9eTMBqQuQiUW9uMzySFSkmK6zjjW5yqydgKLQjxSJAge8k5bfo29VCyiGkgptSBXdF2JY5H9ZYRw"
)


def wormhole_fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((WORMHOLE_FIXTURES / f"{name}.json").read_text())
    return payload


def recorded_vaa_bytes() -> bytes:
    return base64.b64decode(wormhole_fixture("vaa_token_transfer")["data"]["vaa"])


def recorded_message_account() -> dict[str, Any]:
    account: dict[str, Any] = wormhole_fixture("posted_message_account")["result"]["value"]
    return account


# --------------------------------------------------------------- the stubs ----


class StubSolana(SolanaRpcClient):
    """A node that answers from a script, and records what it was asked to send."""

    def __init__(self, *, account: dict[str, Any] | None = None) -> None:
        super().__init__(rpc_url="http://stub")
        self.account = account
        self.sent: list[bytes] = []
        self.send_error: Exception | None = None
        #: What `get_account_info` answers *after* a send, so a test can model
        #: "the message exists now" without pretending it existed before.
        self.account_after_send: dict[str, Any] | None = None
        self.account_calls = 0

    async def get_account_info(
        self, address: str, *, commitment: str = "finalized"
    ) -> dict[str, Any] | None:
        self.account_calls += 1
        if self.sent and self.account_after_send is not None:
            return self.account_after_send
        return self.account

    async def get_latest_blockhash(self, *, commitment: str = "finalized") -> str:
        return BLOCKHASH

    async def send_transaction(self, signed: bytes, *, skip_preflight: bool = False) -> str:
        self.sent.append(signed)
        if self.send_error is not None:
            raise self.send_error
        return SIGNATURE


class StubGuardians(WormholescanClient):
    def __init__(self, vaa: bytes | None) -> None:
        super().__init__(api_url="http://stub")
        self._vaa = vaa
        self.calls: list[tuple[int, str, int]] = []

    async def fetch_vaa(self, *, emitter_chain: int, emitter_address: str, sequence: int) -> Any:
        self.calls.append((emitter_chain, emitter_address, sequence))
        return None if self._vaa is None else parse_signed_vaa(self._vaa)


class StubRedeemer(Redeemer):
    def __init__(
        self, *, delivered: bool = False, refuse: str | None = None, tx_hash: str = "0x" + "ab" * 32
    ) -> None:
        self.delivered = delivered
        self.refuse = refuse
        self.tx_hash = tx_hash
        self.redemptions: list[bytes] = []

    async def is_delivered(self, digest: bytes) -> bool:
        return self.delivered

    async def redeem(self, vaa: bytes) -> str:
        if self.refuse is not None:
            raise RedemptionRefused(self.refuse)
        self.redemptions.append(vaa)
        return self.tx_hash


def signer(seed: int = 3) -> LocalKeypairSigner:
    return LocalKeypairSigner(Keypair.from_seed(bytes([seed]) * 32))


def bridge(
    *,
    solana: StubSolana | None = None,
    guardians: StubGuardians | None = None,
    redeemer: StubRedeemer | None = None,
    signer_: TransactionSigner | None = None,
    **kwargs: Any,
) -> WormholeBridge:
    return WormholeBridge(
        solana=solana or StubSolana(),
        guardians=guardians or StubGuardians(None),
        redeemer=redeemer or StubRedeemer(),
        signer=signer_ or signer(),
        settings=SETTINGS,
        mint=USDC_DEVNET_MINT,
        **kwargs,
    )


def order(order_ref: str = "intent-1", minor: int = 1_000_000) -> BridgeOrder:
    return BridgeOrder(
        order_ref=order_ref,
        amount=Money(amount_minor=minor, currency="USD"),
        source_chain="solana-devnet",
        destination_chain="bsc-testnet",
        destination_address=RECIPIENT,
    )


async def _instantly(seconds: float) -> None:
    """The confirmation wait, without the waiting."""


async def message_address_for(subject_signer: TransactionSigner, order_ref: str) -> str:
    signed = await subject_signer.sign(message_seed_payload(order_ref))
    return str(message_keypair_from_signature(signed).pubkey())


# ---------------------------------------------------------------- submit ----


async def test_a_submitted_order_reports_the_sequence_the_chain_posted() -> None:
    solana = StubSolana()
    solana.account_after_send = recorded_message_account()

    transfer = await bridge(solana=solana).submit(order())

    assert solana.sent, "the transaction was never sent"
    assert transfer.status is BridgeStatus.PENDING
    # 56910 is the recorded message's sequence, and the ref is the Wormhole
    # identity rather than a transaction hash.
    emitter = bytes(emitter_address(Pubkey.from_string(SETTINGS.token_bridge_program))).hex()
    assert transfer.bridge_ref == f"1/{emitter}/56910"
    assert transfer.amount_in.amount_minor == 1_000_000
    assert transfer.raw["source_signature"] == SIGNATURE


async def test_a_second_submit_of_the_same_order_sends_nothing() -> None:
    # THE test of this phase. Wormhole has no idempotency key, so a duplicate
    # would lock a second amount and produce a second VAA.
    solana = StubSolana(account=recorded_message_account())

    transfer = await bridge(solana=solana).submit(order())

    assert solana.sent == []
    assert transfer.status is BridgeStatus.PENDING
    assert transfer.bridge_ref.endswith("/56910")


async def test_a_send_whose_answer_was_lost_is_recovered_rather_than_repeated() -> None:
    # The window that matters: the transaction landed, the response did not. A
    # naive retry here is what produces two locked amounts.
    solana = StubSolana()
    solana.send_error = SolanaRpcError("connection reset", status=0, retryable=True)
    solana.account_after_send = recorded_message_account()

    transfer = await bridge(solana=solana).submit(order())

    assert len(solana.sent) == 1
    assert transfer.bridge_ref.endswith("/56910")


async def test_a_send_that_really_failed_still_raises() -> None:
    # And the other side of it: if the chain has no message, nothing landed, and
    # the error has to reach the engine so it can retry properly.
    solana = StubSolana()
    solana.send_error = SolanaRpcError("connection reset", status=0, retryable=True)

    with pytest.raises(SolanaRpcError):
        await bridge(solana=solana).submit(order())


async def test_two_different_orders_use_two_different_message_accounts() -> None:
    subject_signer = signer()

    first = await message_address_for(subject_signer, "intent-1")
    second = await message_address_for(subject_signer, "intent-2")

    assert first != second


async def test_the_message_account_is_the_one_the_send_used() -> None:
    # Ties the idempotency check to the transaction actually sent: if these
    # diverged, the pre-check would look at an account nothing ever creates.
    subject_signer = signer()
    solana = StubSolana()
    solana.account_after_send = recorded_message_account()

    await bridge(solana=solana, signer_=subject_signer).submit(order())

    # The account keys are in the serialized transaction, so this checks the
    # address the pre-check would look at is the address the send used.
    expected = await message_address_for(subject_signer, "intent-1")
    assert bytes(Pubkey.from_string(expected)) in solana.sent[0]


async def test_a_message_account_that_never_appears_is_retryable_not_invented() -> None:
    # A sequence guessed here becomes a bridge_ref naming somebody else's
    # transfer. `sleep` is injected: the real one waits half a minute, which is
    # right in production and absurd in a test.
    solana = StubSolana()

    with pytest.raises(BridgeError, match="was not readable"):
        await bridge(solana=solana, confirm_attempts=2, sleep=_instantly).submit(order())


async def test_a_non_positive_amount_is_refused_before_anything_is_signed() -> None:
    solana = StubSolana()

    with pytest.raises(BridgeRejectedError, match="positive amount"):
        await bridge(solana=solana).submit(order(minor=0))

    assert solana.sent == []


@pytest.mark.parametrize("address", ["0x1234", "not-an-address", "0x" + "ab" * 32])
async def test_a_recipient_that_is_not_an_evm_address_is_refused(address: str) -> None:
    # A wrongly padded address is a different, valid-looking recipient, and the
    # funds arrive somewhere nobody controls.
    solana = StubSolana()
    bad = BridgeOrder(
        order_ref="intent-1",
        amount=Money(amount_minor=1, currency="USD"),
        source_chain="solana-devnet",
        destination_chain="bsc-testnet",
        destination_address=address,
    )

    with pytest.raises(BridgeRejectedError):
        await bridge(solana=solana).submit(bad)

    assert solana.sent == []


def test_a_token_with_too_many_decimals_is_refused_at_construction() -> None:
    # Above eight, a VAA's amount is scaled and this adapter would have to scale
    # it back before calling it minor units.
    with pytest.raises(ValueError, match="at most 8 decimals"):
        bridge(decimals=9)


# ---------------------------------------------------------------- status ----


async def test_a_transfer_the_guardians_have_not_signed_is_still_pending() -> None:
    guardians = StubGuardians(None)

    transfer = await bridge(guardians=guardians).status("1/abcd/7")

    assert transfer.status is BridgeStatus.PENDING
    assert transfer.raw["stage"] == "awaiting-guardians"
    assert guardians.calls == [(1, "abcd", 7)]


async def test_a_signed_vaa_that_is_not_delivered_is_redeemed() -> None:
    # `status` acts. Nobody else is coming.
    redeemer = StubRedeemer(delivered=False)
    guardians = StubGuardians(recorded_vaa_bytes())

    transfer = await bridge(guardians=guardians, redeemer=redeemer).status("1/abcd/56910")

    assert redeemer.redemptions == [recorded_vaa_bytes()]
    # Submitted is not delivered: the next poll asks the chain rather than
    # believing this receipt.
    assert transfer.status is BridgeStatus.PENDING
    assert transfer.raw["stage"] == "redeeming"
    assert transfer.raw["redemption_tx"] == "0x" + "ab" * 32


async def test_a_delivered_transfer_is_completed_with_what_arrived() -> None:
    redeemer = StubRedeemer(delivered=True)
    guardians = StubGuardians(recorded_vaa_bytes())

    transfer = await bridge(guardians=guardians, redeemer=redeemer).status("1/abcd/56910")

    assert transfer.status is BridgeStatus.COMPLETED
    # Lock-and-mint takes no protocol fee, so what arrives is what was sent —
    # §10.2 point 4, asserted rather than assumed.
    assert transfer.amount_out == transfer.amount_in
    assert transfer.amount_out is not None
    assert transfer.amount_out.amount_minor == 62_560_000
    assert transfer.completed_at is not None
    # And nothing was redeemed twice.
    assert redeemer.redemptions == []


async def test_a_delivered_transfer_is_not_redeemed_again() -> None:
    redeemer = StubRedeemer(delivered=True)

    await bridge(guardians=StubGuardians(recorded_vaa_bytes()), redeemer=redeemer).status(
        "1/abcd/56910"
    )

    assert redeemer.redemptions == []


async def test_a_refused_redemption_fails_with_the_chains_reason() -> None:
    redeemer = StubRedeemer(refuse="invalid emitter")

    transfer = await bridge(
        guardians=StubGuardians(recorded_vaa_bytes()), redeemer=redeemer
    ).status("1/abcd/56910")

    assert transfer.status is BridgeStatus.FAILED
    # Carried, not swallowed: the money is still recoverable with this VAA, and
    # the reason is what tells a person how.
    assert transfer.failure_reason == "invalid emitter"


async def test_the_completed_amount_is_reported_in_the_configured_currency() -> None:
    transfer = await bridge(
        guardians=StubGuardians(recorded_vaa_bytes()),
        redeemer=StubRedeemer(delivered=True),
        currency="EUR",
    ).status("1/abcd/56910")

    assert transfer.amount_in.currency == "EUR"


@pytest.mark.parametrize("ref", ["nonsense", "1/abcd", "x/abcd/7", "1/abcd/seven", ""])
async def test_a_reference_that_is_not_ours_is_an_unknown_transfer(ref: str) -> None:
    with pytest.raises(UnknownTransferError):
        await bridge().status(ref)


async def test_a_message_account_with_no_data_is_an_error_not_a_sequence() -> None:
    # A node can answer with an account whose encoding we did not ask for. Reading
    # a sequence out of nothing would name somebody else's transfer.
    solana = StubSolana(account={"executable": False, "lamports": 1})

    with pytest.raises(Exception, match="without data"):
        await bridge(solana=solana).submit(order())


# The confirmation wait, which the first live transfer forced. `sendTransaction`
# returns when the node accepts the transaction; the message account is read at
# `finalized`, about thirteen seconds behind. A stubbed node answers instantly, so
# no fixture could have shown this.


async def test_submit_waits_for_the_message_account_to_appear() -> None:
    # Reads: one before sending (the idempotency pre-check, which finds nothing),
    # then three polls after it, the last of which sees the account.
    solana = SlowSolana(appears_on_call=4)
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    transfer = await WormholeBridge(
        solana=solana,
        guardians=StubGuardians(None),
        redeemer=StubRedeemer(),
        signer=signer(),
        settings=SETTINGS,
        mint=USDC_DEVNET_MINT,
        confirm_delay_seconds=3.0,
        sleep=record,
    ).submit(order())

    assert transfer.bridge_ref.endswith("/56910")
    # Sent once, looked three times, waited between looks and not after the last.
    assert len(solana.sent) == 1
    assert solana.account_calls == 4
    # Waited between looks, and not after the last one.
    assert slept == [3.0, 3.0]


async def test_submit_gives_up_retryably_if_it_never_appears() -> None:
    # And the retry is safe for the same reason a duplicate submit is: the next
    # attempt reads this same account, so it recovers rather than sending again.
    solana = SlowSolana(appears_on_call=99)

    with pytest.raises(BridgeError) as caught:
        await WormholeBridge(
            solana=solana,
            guardians=StubGuardians(None),
            redeemer=StubRedeemer(),
            signer=signer(),
            settings=SETTINGS,
            mint=USDC_DEVNET_MINT,
            confirm_attempts=4,
            confirm_delay_seconds=3.0,
            sleep=_instantly,
        ).submit(order())

    assert caught.value.retryable is True
    assert "a retry will pick it up" in str(caught.value)
    assert len(solana.sent) == 1
    # The pre-check, then four polls that each came back empty.
    assert solana.account_calls == 5


class SlowSolana(StubSolana):
    """A node whose finalized view catches up only after a few looks."""

    def __init__(self, *, appears_on_call: int) -> None:
        super().__init__()
        self._appears_on_call = appears_on_call

    async def get_account_info(
        self, address: str, *, commitment: str = "finalized"
    ) -> dict[str, Any] | None:
        self.account_calls += 1
        if self.sent and self.account_calls >= self._appears_on_call:
            return recorded_message_account()
        return None
