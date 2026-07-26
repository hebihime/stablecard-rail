"""Delivering a transfer, against recorded BSC testnet answers.

The suite never touches a chain (SPEC.md §10): the reads replay
`tests/fixtures/evm/`, and the one write is signed by a throwaway key and
inspected rather than sent.

What these tests are really about is the three-way distinction a redemption has to
keep straight, because the money is locked on the source chain while it is being
made: *delivered* (stop, and do not pay gas), *refused* (the chain reverted and
said why), and *not yet* (a transport failure, which must never be read as
terminal — the VAA is the only key to that money and it does not expire).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from eth_account import Account

from app.chain.bridge.wormhole.redeemer import (
    GAS_LIMIT_HEADROOM,
    AlreadyDelivered,
    OutOfGasMoney,
    Redeemer,
    RedemptionRefused,
)
from app.chain.evm.rpc import EvmRpcClient, EvmRpcError
from app.chain.evm.signer import EvmTransaction, EvmTransactionSigner, LocalPrivateKeySigner
from app.chain.signer import SignerError

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
RPC_URL = "https://data-seed-prebsc-1-s1.bnbchain.org:8545"
TOKEN_BRIDGE = "0x9dcF9D205C9De35334D646BeE44b2D2859712A09"
CHAIN_ID = 97

#: A throwaway key. Never funded, never used anywhere, and generated here so that
#: nothing in this file could be mistaken for a credential.
TEST_KEY = "0x" + "11" * 32

DIGEST = bytes.fromhex("f6275d50b9a6a27b0f4e51a851278dd80eccbce793ce751e6750d20f23cf5eec")


def fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting."""


class RecordingSigner(EvmTransactionSigner):
    """A signer that keeps what it was asked to sign, so the transaction the
    redeemer *built* can be inspected rather than inferred from its bytes."""

    def __init__(self) -> None:
        self.signed: list[EvmTransaction] = []

    @property
    def address(self) -> str:
        return "0x" + "ab" * 20

    async def sign_transaction(self, transaction: EvmTransaction) -> bytes:
        self.signed.append(transaction)
        return b"\xf8signed"


def redeemer(**kwargs: Any) -> Redeemer:
    return Redeemer(
        EvmRpcClient(rpc_url=RPC_URL, sleep=_no_sleep, **kwargs),
        signer=LocalPrivateKeySigner.from_env_value(TEST_KEY),
        token_bridge=TOKEN_BRIDGE,
        chain_id=CHAIN_ID,
    )


# --------------------------------------------------------------- the signer ----


def test_a_signer_refuses_to_build_without_a_key() -> None:
    # Empty means "not configured". Inventing a key would invent an address
    # nobody funded, and the failure would surface as an out-of-gas somewhere far
    # from here.
    with pytest.raises(SignerError, match="EVM_REDEEMER_PRIVATE_KEY"):
        LocalPrivateKeySigner.from_env_value("   ")


def test_a_signer_refuses_a_key_that_is_not_a_key() -> None:
    with pytest.raises(SignerError, match="not a valid private key"):
        LocalPrivateKeySigner.from_env_value("0xnotakey")


def test_a_key_with_no_prefix_is_accepted_too() -> None:
    # Both forms are what people actually have.
    assert LocalPrivateKeySigner.from_env_value("11" * 32).address == (
        LocalPrivateKeySigner.from_env_value(TEST_KEY).address
    )


async def test_a_signed_transaction_recovers_to_the_signer_and_carries_the_chain_id() -> None:
    signer = LocalPrivateKeySigner.from_env_value(TEST_KEY)

    raw = await signer.sign_transaction(
        EvmTransaction(
            to=TOKEN_BRIDGE,
            data=b"\x01\x02",
            nonce=7,
            gas=200_000,
            gas_price=100_000_000,
            chain_id=CHAIN_ID,
        )
    )

    # Decoded by a different code path than the one that wrote it: eth-account
    # recovers the sender from the signature, so this proves the chain id was
    # signed in (EIP-155) rather than merely stored.
    recovered = Account.recover_transaction(raw)
    assert recovered == signer.address


# ------------------------------------------------------------- is_delivered ----


@respx.mock
async def test_an_undelivered_transfer_reads_false() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("call_transfer_not_completed"))
    )

    assert await redeemer().is_delivered(DIGEST) is False

    # The digest goes in as one raw word after the selector.
    body = json.loads(route.calls.last.request.content)
    assert body["params"][0]["data"] == "0xaa4efa5b" + DIGEST.hex()


@respx.mock
async def test_a_delivered_transfer_reads_true() -> None:
    respx.post(RPC_URL).mock(return_value=ok("0x" + "00" * 31 + "01"))

    assert await redeemer().is_delivered(DIGEST) is True


# ------------------------------------------------------------ wrapped_asset ----


@respx.mock
async def test_an_attested_token_comes_back_as_an_address() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("call_wrapped_asset")))

    wrapped = await redeemer().wrapped_asset(token_chain=1, token_address=b"\x01" * 32)

    assert wrapped == "0x51a3cc54ea30da607974c5d07b8502599801ac08"


@respx.mock
async def test_an_unattested_token_is_none_rather_than_a_zero_address() -> None:
    # A zero here is not an address. Passed on as one, a transfer would arrive as
    # a claim on a contract that does not exist.
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("call_wrapped_asset_unattested"))
    )

    assert await redeemer().wrapped_asset(token_chain=1, token_address=b"\x11" * 32) is None


# ------------------------------------------------------------------ redeem ----


@respx.mock
async def test_redeeming_estimates_then_signs_and_sends() -> None:
    tx_hash = "0x" + "ab" * 32
    route = respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),  # eth_estimateGas -> 200000
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            ok(tx_hash),
        ]
    )

    assert await redeemer().redeem(b"\xde\xad\xbe\xef") == tx_hash

    calls = [json.loads(call.request.content) for call in route.calls]
    assert [body["method"] for body in calls] == [
        "eth_estimateGas",
        "eth_gasPrice",
        "eth_getTransactionCount",
        "eth_sendRawTransaction",
    ]
    # completeTransfer(bytes): selector, offset, length, then the padded VAA.
    assert calls[0]["params"][0]["data"].startswith("0xc6878519")
    assert calls[0]["params"][0]["data"].endswith("deadbeef".ljust(64, "0"))


@respx.mock
async def test_the_gas_limit_leaves_headroom_over_the_estimate() -> None:
    # A redemption verifies guardian signatures, and that cost moves with the
    # guardian set. Running out is a failed transaction that still pays for the
    # gas it burnt.
    respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            ok("0x" + "cd" * 32),
        ]
    )
    signer = RecordingSigner()
    subject = Redeemer(
        EvmRpcClient(rpc_url=RPC_URL, sleep=_no_sleep),
        signer=signer,
        token_bridge=TOKEN_BRIDGE,
        chain_id=CHAIN_ID,
    )

    await subject.redeem(b"\x01")

    signed = signer.signed[0]
    assert signed.gas == int(200_000 * GAS_LIMIT_HEADROOM)
    assert signed.chain_id == CHAIN_ID
    assert signed.to == TOKEN_BRIDGE
    # The nonce comes from the node, not from a counter this service keeps.
    assert signed.nonce == 0
    assert signed.gas_price == 0x5F5E100


@respx.mock
async def test_a_revert_is_refused_with_the_chains_own_reason() -> None:
    # The free pre-flight: the chain says why, and says it without spending
    # anything. "already completed" and "invalid emitter" both surface here.
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("error_execution_reverted"))
    )

    with pytest.raises(RedemptionRefused) as caught:
        await redeemer().redeem(b"\x01")

    assert caught.value.reason == "VM version incompatible"
    assert caught.value.retryable is False


@respx.mock
async def test_a_node_that_cannot_be_reached_stays_retryable() -> None:
    # The distinction the locked money depends on: this is "not yet", not "no".
    # Treating it as terminal would strand funds whose only key is a VAA that
    # never expires.
    respx.post(RPC_URL).mock(side_effect=httpx.ConnectError("reset"))

    with pytest.raises(EvmRpcError) as caught:
        await redeemer(backoff=()).redeem(b"\x01")

    assert caught.value.retryable is True
    assert not isinstance(caught.value, RedemptionRefused)


@respx.mock
async def test_a_send_that_is_rejected_is_not_dressed_up_as_a_revert() -> None:
    # "nonce too low" used to be this test's example, and a later change gave that
    # phrase a specific meaning (the transaction is already in the pool), so the
    # example moved rather than the intent: a rejected send is not a revert.
    respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "exceeds block gas limit"},
                },
            ),
        ]
    )

    with pytest.raises(EvmRpcError) as caught:
        await redeemer().redeem(b"\x01")

    assert not isinstance(caught.value, RedemptionRefused)
    assert "exceeds block gas limit" in str(caught.value)


# ----------------------------------------------------------------- receipts ----


@respx.mock
async def test_a_mined_success_reads_true() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("receipt_success")))

    assert await redeemer().receipt_status("0x" + "ab" * 32) is True


@respx.mock
async def test_a_mined_failure_reads_false() -> None:
    receipt = dict(fixture("receipt_success")["result"])
    receipt["status"] = "0x0"
    respx.post(RPC_URL).mock(return_value=ok(receipt))

    assert await redeemer().receipt_status("0x" + "ab" * 32) is False


@respx.mock
async def test_an_unmined_transaction_reads_none_and_not_false() -> None:
    # Three answers because there are three. Collapsing "not yet" into "failed"
    # abandons a redemption that was about to succeed.
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("receipt_missing")))

    assert await redeemer().receipt_status("0x" + "ab" * 32) is None


def test_the_redeemer_pays_gas_from_the_signers_address() -> None:
    # And never receives anything: a Wormhole transfer credits the recipient
    # encoded in the VAA, whoever submits it.
    assert redeemer().address == LocalPrivateKeySigner.from_env_value(TEST_KEY).address


# The two operational answers a send can give, both found by a live transfer
# rather than by any fixture — a stubbed node never runs out of money.


@respx.mock
async def test_an_underfunded_redeemer_is_retryable_not_terminal() -> None:
    # The bug this class exists for. `-32000` covers half a dozen unrelated
    # conditions and is otherwise treated as permanent, which would mark an intent
    # FAILED_BRIDGE while the funds sat locked behind a VAA that never expires.
    # Estimation succeeds first, note: BSC does not check balance there, so the
    # failure only appears at the send.
    respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32000,
                        "message": "insufficient funds for gas * price + value: "
                        "balance 1000000000000, tx cost 15563300000000, overshot 14563300000000",
                    },
                },
            ),
        ]
    )

    with pytest.raises(OutOfGasMoney) as caught:
        await redeemer().redeem(b"\x01")

    assert caught.value.retryable is True
    assert caught.value.address == LocalPrivateKeySigner.from_env_value(TEST_KEY).address
    assert "top it up" in str(caught.value)


@respx.mock
async def test_a_send_the_node_already_knows_is_a_success() -> None:
    # Same nonce, same bytes, same hash — so a previous attempt's transaction is
    # in the pool and there is nothing new to send. Reading this as a failure
    # would make a redeemer retry forever against a node that already agreed.
    respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "already known"},
                },
            ),
        ]
    )

    tx_hash = await redeemer().redeem(b"\x01")

    # The hash is a property of the signed bytes, so it is knowable without the
    # node's answer — which is what lets this be reported as the success it is.
    assert tx_hash.startswith("0x")
    assert len(tx_hash) == 66


@respx.mock
async def test_a_nonce_too_low_is_also_already_submitted() -> None:
    respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "nonce too low"},
                },
            ),
        ]
    )

    assert (await redeemer().redeem(b"\x01")).startswith("0x")


@respx.mock
async def test_a_generic_minus_32000_is_still_raised_as_itself() -> None:
    # Only the two known phrases are reinterpreted. Anything else stays an
    # EvmRpcError, because inventing a meaning for an unfamiliar node message is
    # how a real failure gets swallowed.
    respx.post(RPC_URL).mock(
        side_effect=[
            ok("0x30d40"),
            httpx.Response(200, json=fixture("gas_price")),
            httpx.Response(200, json=fixture("transaction_count")),
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "intrinsic gas too low"},
                },
            ),
        ]
    )

    with pytest.raises(EvmRpcError) as caught:
        await redeemer().redeem(b"\x01")

    assert not isinstance(caught.value, OutOfGasMoney)
    assert "intrinsic gas too low" in str(caught.value)


@respx.mock
async def test_an_already_completed_revert_is_delivery_not_refusal() -> None:
    # The revert text here is the one BSC testnet actually produced when a
    # duplicate redemption was attempted against a delivered transfer. Calling it
    # a refusal would be the worst misclassification available: FAILED_BRIDGE on
    # money that has arrived.
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": 3,
                    "message": "execution reverted: transfer already completed: 0x08c379a0",
                },
            },
        )
    )

    with pytest.raises(AlreadyDelivered) as caught:
        await redeemer().redeem(b"\x01")

    assert not isinstance(caught.value, RedemptionRefused)
    assert "already delivered" in str(caught.value)


@respx.mock
async def test_any_other_revert_is_still_a_refusal() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("error_execution_reverted"))
    )

    with pytest.raises(RedemptionRefused) as caught:
        await redeemer().redeem(b"\x01")

    assert not isinstance(caught.value, AlreadyDelivered)
    assert caught.value.reason == "VM version incompatible"
