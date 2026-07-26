"""The destination chain's client, against recorded BSC testnet responses.

Every fixture here came off `data-seed-prebsc-1-s1.bnbchain.org` (see the fixtures
README). The suite never calls it: SPEC.md §10.

These tests are mostly about the three answers that do not look like what they are
— an error inside a 200, a `null` receipt inside a 200, and a revert that is a
refusal rather than a "try again" — because each one, read wrongly, breaks a
redemption in a way that looks like something else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.chain.evm.abi import (
    IS_TRANSFER_COMPLETED,
    WRAPPED_ASSET,
    ZERO_ADDRESS,
    decode_address,
    decode_bool,
    encode_bytes32_arg,
    encode_bytes_arg,
    encode_uint16_and_bytes32,
    selector,
)
from app.chain.evm.rpc import RETRYABLE_STATUSES, EvmRpcClient, EvmRpcError

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
RPC_URL = "https://data-seed-prebsc-1-s1.bnbchain.org:8545"
TOKEN_BRIDGE = "0x9dcF9D205C9De35334D646BeE44b2D2859712A09"


def fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting."""


def client(**kwargs: Any) -> EvmRpcClient:
    return EvmRpcClient(rpc_url=RPC_URL, sleep=_no_sleep, **kwargs)


# ------------------------------------------------------------- quantities ----


@respx.mock
async def test_a_chain_id_is_hex_and_comes_back_an_int() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("chain_id")))

    # 0x61. A chain id read as a string compares equal to nothing.
    assert await client().chain_id() == 97


@respx.mock
async def test_a_gas_price_is_read_as_wei() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("gas_price")))

    assert await client().gas_price() == 0x5F5E100


@respx.mock
async def test_the_nonce_is_asked_for_pending_not_latest() -> None:
    # `latest` ignores this sender's own transactions still in the pool, so two
    # redemptions in a row would be signed with one nonce and one would vanish.
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("transaction_count"))
    )

    assert await client().transaction_count("0x0000000000000000000000000000000000000001") == 0

    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "eth_getTransactionCount"
    assert body["params"][1] == "pending"


@respx.mock
async def test_a_quantity_that_is_not_hex_is_an_error_not_a_zero() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "later"})
    )

    with pytest.raises(EvmRpcError, match="not a quantity"):
        await client().gas_price()


@respx.mock
async def test_a_quantity_that_is_not_a_string_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 12})
    )

    with pytest.raises(EvmRpcError, match="not a quantity"):
        await client().chain_id()


# ---------------------------------------------------------------- reading ----


@respx.mock
async def test_an_attested_token_comes_back_as_an_address() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("call_wrapped_asset"))
    )

    word = await client().call_contract(
        to=TOKEN_BRIDGE,
        data=selector(WRAPPED_ASSET) + encode_uint16_and_bytes32(1, b"\x01" * 32),
    )

    # The wrapped devnet USDC that made this route usable without an attestation
    # step of our own (docs/ARCHITECTURE.md §10.1).
    assert decode_address(word) == "0x51a3cc54ea30da607974c5d07b8502599801ac08"
    body = json.loads(route.calls.last.request.content)
    assert body["params"][0]["to"] == TOKEN_BRIDGE
    assert body["params"][1] == "latest"


@respx.mock
async def test_a_token_nobody_attested_is_a_zero_inside_a_200() -> None:
    # The trap. Not an error, not a null — a perfectly ordinary success whose
    # value happens to mean "no such token". Reading it as an address sends a
    # transfer to 0x0, which on this bridge is a burn.
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("call_wrapped_asset_unattested"))
    )

    word = await client().call_contract(
        to=TOKEN_BRIDGE, data=selector(WRAPPED_ASSET) + encode_uint16_and_bytes32(1, b"\x11" * 32)
    )

    assert decode_address(word) == ZERO_ADDRESS


@respx.mock
async def test_an_undelivered_transfer_reads_false() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("call_transfer_not_completed"))
    )

    word = await client().call_contract(
        to=TOKEN_BRIDGE,
        data=selector(IS_TRANSFER_COMPLETED) + encode_bytes32_arg(b"\x22" * 32),
    )

    assert decode_bool(word) is False


@respx.mock
async def test_a_call_that_answers_with_a_non_string_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    )

    with pytest.raises(EvmRpcError, match="not hex data"):
        await client().call_contract(to=TOKEN_BRIDGE, data=b"\x00")


# --------------------------------------------------------------- receipts ----


@respx.mock
async def test_a_receipt_carries_its_status_and_logs() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("receipt_success")))

    receipt = await client().transaction_receipt("0x" + "cd" * 32)

    assert receipt is not None
    assert receipt["status"] == "0x1"
    assert receipt["logs"]


@respx.mock
async def test_an_unmined_transaction_is_none_and_not_a_failure() -> None:
    # `result: null` inside a 200 — the same shape, and the same trap, as
    # `getTransaction` on Solana. It means "not yet".
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("receipt_missing")))

    assert await client().transaction_receipt("0x" + "ab" * 32) is None


@respx.mock
async def test_a_receipt_that_is_not_an_object_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})
    )

    with pytest.raises(EvmRpcError, match="not an object"):
        await client().transaction_receipt("0x" + "ab" * 32)


# ---------------------------------------------------------------- sending ----


@respx.mock
async def test_sending_returns_the_hash_and_sends_hex() -> None:
    tx_hash = "0x" + "11" * 32
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": tx_hash})
    )

    assert await client().send_raw_transaction(b"\xde\xad\xbe\xef") == tx_hash

    body = json.loads(route.calls.last.request.content)
    assert body["params"] == ["0xdeadbeef"]


@respx.mock
async def test_a_send_that_answers_with_a_non_string_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
    )

    with pytest.raises(EvmRpcError, match="not a hash"):
        await client().send_raw_transaction(b"\x01")


@respx.mock
async def test_a_send_is_retried_when_nothing_answers() -> None:
    # Safe precisely because the bytes are signed: the nonce and therefore the
    # hash are fixed, so a duplicate cannot become a second transaction.
    tx_hash = "0x" + "22" * 32
    route = respx.post(RPC_URL).mock(
        side_effect=[
            httpx.ConnectError("reset"),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": tx_hash}),
        ]
    )

    assert await client().send_raw_transaction(b"\x01") == tx_hash
    assert route.call_count == 2


@respx.mock
async def test_gas_estimation_asks_as_the_sender() -> None:
    # `from` matters: a redemption is estimated against the account that will pay,
    # and some reverts depend on the caller.
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x30d40"})
    )

    sender = "0x" + "ab" * 20
    assert await client().estimate_gas(to=TOKEN_BRIDGE, data=b"\x01", sender=sender) == 200_000

    body = json.loads(route.calls.last.request.content)
    assert body["params"][0]["from"] == sender


# ----------------------------------------------------------------- errors ----


@respx.mock
async def test_a_revert_is_not_retried_and_keeps_the_contracts_words() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("error_execution_reverted"))
    )

    with pytest.raises(EvmRpcError) as caught:
        await client().call_contract(
            to=TOKEN_BRIDGE, data=selector("completeTransfer(bytes)") + encode_bytes_arg(b"\xde")
        )

    error = caught.value
    assert error.reverted
    assert error.retryable is False
    # The reason arrives twice in one string — plain, then ABI-encoded. Only the
    # half a person can read is kept.
    assert error.revert_reason == "VM version incompatible"
    assert "0x08c379a0" not in (error.revert_reason or "")
    assert route.call_count == 1


@respx.mock
async def test_a_complaint_about_the_request_is_not_retried() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("error_method_not_found"))
    )

    with pytest.raises(EvmRpcError) as caught:
        await client().call("eth_nonsense", [])

    assert caught.value.code == -32601
    assert caught.value.retryable is False
    assert caught.value.revert_reason is None
    assert route.call_count == 1


@respx.mock
async def test_a_limit_exceeded_code_is_retried() -> None:
    route = respx.post(RPC_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit"}},
            ),
            httpx.Response(200, json=fixture("gas_price")),
        ]
    )

    assert await client().gas_price() == 0x5F5E100
    assert route.call_count == 2


@respx.mock
async def test_a_generic_node_complaint_is_left_to_the_caller() -> None:
    # -32000 covers "nonce too low", "already known" and "insufficient funds" —
    # answers about this request that the redeemer has to read, not repeat.
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nonce too low"}},
        )
    )

    with pytest.raises(EvmRpcError) as caught:
        await client().send_raw_transaction(b"\x01")

    assert caught.value.retryable is False
    assert "nonce too low" in str(caught.value)
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
async def test_a_retryable_status_is_retried_then_succeeds(status: int) -> None:
    route = respx.post(RPC_URL).mock(
        side_effect=[
            httpx.Response(status, text="slow down"),
            httpx.Response(200, json=fixture("chain_id")),
        ]
    )

    assert await client().chain_id() == 97
    assert route.call_count == 2


@respx.mock
async def test_a_retryable_status_that_never_clears_is_retryable_when_it_gives_up() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(429, text="slow down"))

    with pytest.raises(EvmRpcError) as caught:
        await client(backoff=(0.0,)).chain_id()

    assert caught.value.status == 429
    assert caught.value.retryable is True


@respx.mock
async def test_a_body_that_is_not_json_rpc_says_so() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(EvmRpcError, match="not JSON-RPC"):
        await client().chain_id()


@respx.mock
async def test_a_failing_status_with_no_error_body_is_still_an_error() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(400, json={"jsonrpc": "2.0", "id": 1}))

    with pytest.raises(EvmRpcError) as caught:
        await client().chain_id()

    assert caught.value.status == 400
    assert caught.value.retryable is False


@respx.mock
async def test_nothing_answering_at_all_is_retryable() -> None:
    respx.post(RPC_URL).mock(side_effect=httpx.ConnectTimeout("no route"))

    with pytest.raises(EvmRpcError) as caught:
        await client(backoff=()).chain_id()

    assert caught.value.status == 0
    assert caught.value.retryable is True


@respx.mock
async def test_an_error_that_is_not_an_object_still_fails_cleanly() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": "broken"})
    )

    with pytest.raises(EvmRpcError, match="'broken'"):
        await client().chain_id()


# ------------------------------------------------------------ abi encoding ---


def test_a_selector_is_four_bytes_of_the_canonical_signature() -> None:
    # Pinned values, so a change to the encoder is visible rather than silent.
    assert selector("completeTransfer(bytes)").hex() == "c6878519"
    assert selector(WRAPPED_ASSET).hex() == "1ff1e286"
    assert selector(IS_TRANSFER_COMPLETED).hex() == "aa4efa5b"


def test_static_arguments_are_one_word_each() -> None:
    encoded = encode_uint16_and_bytes32(4, b"\xab" * 32)

    assert len(encoded) == 64
    assert encoded[:32] == (4).to_bytes(32, "big")
    assert encoded[32:] == b"\xab" * 32


def test_a_number_too_big_for_a_uint16_is_refused() -> None:
    with pytest.raises(ValueError, match="uint16"):
        encode_uint16_and_bytes32(70_000, b"\x00" * 32)


def test_a_short_bytes32_is_refused_rather_than_padded() -> None:
    # Padding would quietly change which token or transfer is being asked about.
    with pytest.raises(ValueError, match="32 bytes"):
        encode_bytes32_arg(b"\x01" * 20)


def test_dynamic_bytes_carry_an_offset_a_length_and_padding() -> None:
    encoded = encode_bytes_arg(b"\x01\x02\x03")

    assert encoded[:32] == (32).to_bytes(32, "big")
    assert encoded[32:64] == (3).to_bytes(32, "big")
    assert encoded[64:67] == b"\x01\x02\x03"
    assert len(encoded) == 96  # padded up to a whole word
    assert encoded[67:] == b"\x00" * 29


def test_dynamic_bytes_of_an_exact_word_length_get_no_extra_padding() -> None:
    assert len(encode_bytes_arg(b"\x07" * 64)) == 128


def test_decoding_refuses_anything_that_is_not_one_word() -> None:
    with pytest.raises(ValueError, match="single 32-byte word"):
        decode_address("0x1234")
    with pytest.raises(ValueError, match="single 32-byte word"):
        decode_bool("0x" + "00" * 64)


def test_a_bool_is_true_for_any_non_zero_word() -> None:
    assert decode_bool("0x" + "00" * 31 + "01") is True
    assert decode_bool("0x" + "00" * 32) is False
