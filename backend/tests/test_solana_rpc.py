"""The JSON-RPC client, against recorded devnet responses.

Every fixture here came off `api.devnet.solana.com` (see the fixtures README).
The suite never calls it: SPEC.md §10.

What these tests are really about is the two answers that do not look like what
they are — an error inside a 200, and a `null` result inside a 200 — because a
client that reads either one wrongly makes the watcher skip a deposit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.chain.rpc import RETRYABLE_STATUSES, SolanaRpcClient, SolanaRpcError

FIXTURES = Path(__file__).parent / "fixtures" / "solana"
RPC_URL = "https://api.devnet.solana.com"


def fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting. The delays are asserted, not endured."""


def client(**kwargs: Any) -> SolanaRpcClient:
    return SolanaRpcClient(rpc_url=RPC_URL, sleep=_no_sleep, **kwargs)


@respx.mock
async def test_signatures_come_back_newest_first() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("signatures_for_deposit_account"))
    )

    entries = await client().get_signatures_for_address(
        "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"
    )

    assert [entry["slot"] for entry in entries] == sorted(
        (entry["slot"] for entry in entries), reverse=True
    )
    assert all(entry["confirmationStatus"] == "finalized" for entry in entries)


@respx.mock
async def test_the_cursor_is_sent_as_until() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("signatures_none"))
    )

    await client().get_signatures_for_address("addr", until="sig-1", limit=5)

    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "getSignaturesForAddress"
    assert body["params"][1] == {"limit": 5, "commitment": "finalized", "until": "sig-1"}


@respx.mock
async def test_an_address_with_no_history_is_an_empty_list_not_an_error() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("signatures_none")))

    assert await client().get_signatures_for_address("addr") == []


@respx.mock
async def test_a_transaction_is_requested_in_parsed_form_and_tolerates_versions() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("transaction_transfer_checked"))
    )

    transaction = await client().get_transaction("sig")

    assert transaction is not None
    options = json.loads(route.calls.last.request.content)["params"][1]
    assert options["encoding"] == "jsonParsed"
    # Without this the node refuses every versioned transaction, which is most.
    assert options["maxSupportedTransactionVersion"] == 0
    assert options["commitment"] == "finalized"


@respx.mock
async def test_an_unknown_signature_is_none_rather_than_an_error() -> None:
    # `result: null` inside a 200. Nothing about the status or the envelope says
    # "missing", and treating it as a failed transfer would be worse than either:
    # the watcher would step past a deposit that is about to appear.
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("transaction_not_found"))
    )

    assert await client().get_transaction("sig") is None


@respx.mock
async def test_an_rpc_error_inside_a_200_is_still_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("error_invalid_address"))
    )

    with pytest.raises(SolanaRpcError) as caught:
        await client().get_signatures_for_address("not-a-pubkey")

    assert caught.value.code == -32602
    assert caught.value.status == 200
    # A complaint about the request will not improve on a second attempt.
    assert caught.value.retryable is False


@respx.mock
async def test_a_rate_limit_is_retried_and_then_reported_as_retryable() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("error_rate_limited"))
    )

    with pytest.raises(SolanaRpcError) as caught:
        await client(backoff=(0.0, 0.0)).get_signatures_for_address("addr")

    assert route.call_count == 3  # the first attempt, then both backoff steps
    assert caught.value.code == 429
    assert caught.value.retryable is True


@respx.mock
async def test_a_rate_limit_that_clears_is_not_an_error_at_all() -> None:
    respx.post(RPC_URL).mock(
        side_effect=[
            httpx.Response(200, json=fixture("error_rate_limited")),
            httpx.Response(200, json=fixture("signatures_none")),
        ]
    )

    assert await client(backoff=(0.0,)).get_signatures_for_address("addr") == []


@respx.mock
@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
async def test_a_retryable_status_is_retried(status: int) -> None:
    respx.post(RPC_URL).mock(
        side_effect=[
            httpx.Response(status, json={"error": "nope"}),
            httpx.Response(200, json=fixture("signatures_none")),
        ]
    )

    assert await client(backoff=(0.0,)).get_signatures_for_address("addr") == []


@respx.mock
async def test_a_status_that_keeps_failing_is_reported_as_retryable() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(503, text="upstream down"))

    with pytest.raises(SolanaRpcError) as caught:
        await client(backoff=(0.0,)).get_signatures_for_address("addr")

    assert caught.value.status == 503
    assert caught.value.retryable is True


@respx.mock
async def test_a_permanent_status_is_not_retried() -> None:
    route = respx.post(RPC_URL).mock(return_value=httpx.Response(400, text="bad request"))

    with pytest.raises(SolanaRpcError) as caught:
        await client(backoff=(0.0,)).get_signatures_for_address("addr")

    assert route.call_count == 1
    assert caught.value.retryable is False


@respx.mock
async def test_nothing_answering_at_all_is_retryable() -> None:
    respx.post(RPC_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(SolanaRpcError) as caught:
        await client(backoff=(0.0,)).get_signatures_for_address("addr")

    # Status 0 is the "nothing answered" convention both issuer clients use.
    assert caught.value.status == 0
    assert caught.value.retryable is True


@respx.mock
async def test_a_transport_failure_that_clears_is_retried() -> None:
    respx.post(RPC_URL).mock(
        side_effect=[
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, json=fixture("signatures_none")),
        ]
    )

    assert await client(backoff=(0.0,)).get_signatures_for_address("addr") == []


@respx.mock
async def test_a_body_that_is_not_json_rpc_is_an_error_not_a_crash() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, text="<html>proxy error</html>"))

    with pytest.raises(SolanaRpcError, match="not JSON-RPC"):
        await client().get_signatures_for_address("addr")


@respx.mock
@pytest.mark.parametrize(
    ("method", "shape"),
    [("getSignaturesForAddress", {"jsonrpc": "2.0", "id": 1, "result": {"not": "a list"}})],
)
async def test_a_result_of_the_wrong_shape_is_rejected(method: str, shape: dict[str, Any]) -> None:
    # Defensive, but the alternative is a TypeError three frames into the watcher.
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=shape))

    with pytest.raises(SolanaRpcError, match="not a list"):
        await client().get_signatures_for_address("addr")


@respx.mock
async def test_a_transaction_result_of_the_wrong_shape_is_rejected() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": ["nope"]})
    )

    with pytest.raises(SolanaRpcError, match="not an object"):
        await client().get_transaction("sig")


@respx.mock
async def test_an_error_that_is_not_an_object_is_still_reported() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": "plain string"})
    )

    with pytest.raises(SolanaRpcError, match="plain string"):
        await client().get_signatures_for_address("addr")


@respx.mock
async def test_a_failing_status_with_no_rpc_error_body_is_reported() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(404, json={"jsonrpc": "2.0", "id": 1}))

    with pytest.raises(SolanaRpcError, match="failed"):
        await client().get_signatures_for_address("addr")


# The four calls phase 6 added, for building and sending a transaction rather
# than only watching. Two of the answers are recorded; `sendTransaction` and
# `simulateTransaction` are authored, because recording them would mean writing
# to the chain and the fixtures README draws that line deliberately.


@respx.mock
async def test_an_account_that_exists_comes_back_with_its_data() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("account_info_program")))

    account = await client().get_account_info("DZnkkTmCiFWfYTfT41X3Rd1kDgozqzxWaHqsw6W4x2oe")

    assert account is not None
    assert account["executable"] is True
    assert account["data"][1] == "base64"


@respx.mock
async def test_an_account_nobody_created_is_none_not_an_error() -> None:
    # `value: null` inside a 200 — and the bridge adapter leans on it hardest:
    # it means "nothing has been submitted for this order yet".
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, json=fixture("account_info_missing")))

    assert await client().get_account_info("6Be1VPtVP9tcx9JN8HcPXcndEriVQofUGAwLTFZLuWxG") is None


@respx.mock
async def test_an_account_info_result_that_is_not_an_object_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "gone"})
    )

    with pytest.raises(SolanaRpcError, match="not an object"):
        await client().get_account_info("addr")


@respx.mock
async def test_an_account_value_that_is_not_an_account_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": "0x01"}}
        )
    )

    with pytest.raises(SolanaRpcError, match="not an account"):
        await client().get_account_info("addr")


@respx.mock
async def test_a_blockhash_is_read_out_of_the_context_envelope() -> None:
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json=fixture("latest_blockhash"))
    )

    blockhash = await client().get_latest_blockhash()

    assert blockhash == "DTdt9NMNuAaoftWPLpJNYUNqZG3Amu6Zw1NiuDsJLGUg"
    # `finalized`, not `processed`: a blockhash from a slot that gets dropped
    # takes the transaction with it.
    body = json.loads(route.calls.last.request.content)
    assert body["params"][0] == {"commitment": "finalized"}


@respx.mock
async def test_a_missing_blockhash_is_an_error_rather_than_an_empty_string() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": {}}})
    )

    with pytest.raises(SolanaRpcError, match="no blockhash"):
        await client().get_latest_blockhash()


@respx.mock
async def test_a_blockhash_result_that_is_not_an_object_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
    )

    with pytest.raises(SolanaRpcError, match="not an object"):
        await client().get_latest_blockhash()


@respx.mock
async def test_a_transaction_is_sent_base64_encoded() -> None:
    signature = (
        "5Fig5NhWgYLW9eTMBqQuQiUW9uMzySFSkmK6zjjW5yqydgKLQjxSJAge8k5bfo29VCyiGkgptSBXdF2JY5H9ZYRw"
    )
    route = respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": signature})
    )

    assert await client().send_transaction(b"\x01\x02\x03") == signature

    body = json.loads(route.calls.last.request.content)
    assert body["params"][0] == "AQID"
    assert body["params"][1]["encoding"] == "base64"
    # One retry inside the node: this service does its own, and a node that
    # retries for a minute hides a failure.
    assert body["params"][1]["maxRetries"] == 1


@respx.mock
async def test_a_send_that_answers_with_a_non_signature_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    )

    with pytest.raises(SolanaRpcError, match="not a signature"):
        await client().send_transaction(b"\x01")


@respx.mock
async def test_a_simulation_comes_back_with_its_logs_and_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "context": {"slot": 1},
                    "value": {
                        "err": None,
                        "logs": ["Program log: Sequence: 7"],
                        "unitsConsumed": 9,
                    },
                },
            },
        )
    )

    simulated = await client().simulate_transaction(b"\x01")

    assert simulated["err"] is None
    assert simulated["logs"] == ["Program log: Sequence: 7"]


@respx.mock
async def test_a_simulation_with_no_value_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}}}
        )
    )

    with pytest.raises(SolanaRpcError, match="no value"):
        await client().simulate_transaction(b"\x01")


@respx.mock
async def test_a_simulation_result_that_is_not_an_object_is_an_error() -> None:
    respx.post(RPC_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})
    )

    with pytest.raises(SolanaRpcError, match="not an object"):
        await client().simulate_transaction(b"\x01")
