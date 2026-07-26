"""Asking the guardian API, against its recorded answers.

The test that matters here is the 404 one. A transfer whose VAA is not signed yet
is the ordinary state of every transfer for its first few seconds, and Wormholescan
reports it with a status code that reads like a failure. Getting that wrong fails
healthy transfers in production and passes every unit test written against a happy
path, which is why the 404 body is a recorded fixture rather than a mock.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.chain.bridge.wormhole.client import WormholeApiError, WormholescanClient
from app.chain.bridge.wormhole.vaa import MalformedVaaError

FIXTURES = Path(__file__).parent / "fixtures" / "wormhole"
API_URL = "https://api.testnet.wormholescan.io"
EMITTER = "3b26409f8aaded3f5ddca184695aa6a0fa829b0c85caf84856324896d214ca98"
SEQUENCE = 56910
PATH = f"/api/v1/vaas/1/{EMITTER}/{SEQUENCE}"


def fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


async def _no_sleep(seconds: float) -> None:
    """Backoff without the waiting."""


def client(**kwargs: Any) -> WormholescanClient:
    return WormholescanClient(api_url=API_URL, sleep=_no_sleep, **kwargs)


async def fetch(**kwargs: Any) -> Any:
    return await client(**kwargs).fetch_vaa(
        emitter_chain=1, emitter_address=EMITTER, sequence=SEQUENCE
    )


@respx.mock
async def test_a_signed_vaa_comes_back_parsed() -> None:
    route = respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=fixture("vaa_token_transfer"))
    )

    vaa = await fetch()

    assert vaa is not None
    assert vaa.sequence == SEQUENCE
    assert vaa.emitter_address.hex() == EMITTER
    assert route.call_count == 1


@respx.mock
async def test_a_vaa_the_guardians_have_not_signed_is_none_not_an_error() -> None:
    # The whole point of this module. A 404 here is "not yet" — not "no such
    # transfer", and not a failure. Read either other way, a healthy transfer
    # either fails or is abandoned.
    respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(404, json=fixture("vaa_not_signed_yet"))
    )

    assert await fetch() is None


@respx.mock
async def test_a_404_is_not_retried() -> None:
    # It is an answer, and asking again immediately just costs a request against
    # a public rate limit.
    route = respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(404, json=fixture("vaa_not_signed_yet"))
    )

    await fetch()

    assert route.call_count == 1


@respx.mock
async def test_the_collection_form_of_the_answer_is_accepted() -> None:
    # The same path has answered with a list in the past. One sequence is one
    # VAA, so the first element is not a guess.
    record = fixture("vaa_token_transfer")["data"]
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(200, json={"data": [record]}))

    vaa = await fetch()

    assert vaa is not None and vaa.sequence == SEQUENCE


@respx.mock
async def test_an_empty_collection_is_treated_as_no_vaa_yet() -> None:
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(200, json={"data": []}))

    with pytest.raises(WormholeApiError, match="without a VAA"):
        await fetch()


@respx.mock
async def test_a_record_with_no_vaa_field_is_refused() -> None:
    respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(200, json={"data": {"sequence": SEQUENCE}})
    )

    with pytest.raises(WormholeApiError, match="without a VAA"):
        await fetch()


@respx.mock
async def test_base64_that_does_not_decode_is_refused() -> None:
    respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(200, json={"data": {"vaa": "not base64!!"}})
    )

    with pytest.raises(WormholeApiError, match="unusable base64"):
        await fetch()


@respx.mock
async def test_bytes_that_decode_but_are_not_a_vaa_fail_as_malformed() -> None:
    respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"data": {"vaa": base64.b64encode(b"\x01\x02").decode()}}
        )
    )

    with pytest.raises(MalformedVaaError):
        await fetch()


@respx.mock
async def test_a_digest_that_disagrees_with_the_bytes_is_refused() -> None:
    # Not a trust decision: the digest this service uses is always the one it
    # computes. A disagreement means the bytes are not what the explorer indexed,
    # and money-moving bytes that fail their own index are worth refusing.
    record = dict(fixture("vaa_token_transfer")["data"])
    record["digest"] = "00" * 32
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(200, json={"data": record}))

    with pytest.raises(WormholeApiError, match="reported digest"):
        await fetch()


@respx.mock
async def test_a_digest_with_an_0x_prefix_still_matches() -> None:
    record = dict(fixture("vaa_token_transfer")["data"])
    record["digest"] = "0x" + record["digest"]
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(200, json={"data": record}))

    assert await fetch() is not None


@respx.mock
async def test_a_rate_limit_is_retried_then_succeeds() -> None:
    route = respx.get(f"{API_URL}{PATH}").mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(200, json=fixture("vaa_token_transfer")),
        ]
    )

    assert await fetch() is not None
    assert route.call_count == 2


@respx.mock
async def test_a_rate_limit_that_never_clears_is_retryable_when_it_gives_up() -> None:
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(503, text="unavailable"))

    with pytest.raises(WormholeApiError) as caught:
        await fetch(backoff=(0.0,))

    assert caught.value.status == 503
    assert caught.value.retryable is True


@respx.mock
async def test_a_client_error_that_is_not_a_404_is_not_retryable() -> None:
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(400, text="bad emitter"))

    with pytest.raises(WormholeApiError) as caught:
        await fetch()

    assert caught.value.status == 400
    assert caught.value.retryable is False


@respx.mock
async def test_nothing_answering_at_all_is_retryable() -> None:
    respx.get(f"{API_URL}{PATH}").mock(side_effect=httpx.ConnectTimeout("no route"))

    with pytest.raises(WormholeApiError) as caught:
        await fetch(backoff=())

    assert caught.value.status == 0
    assert caught.value.retryable is True


@respx.mock
async def test_a_transport_failure_is_retried_before_giving_up() -> None:
    route = respx.get(f"{API_URL}{PATH}").mock(
        side_effect=[
            httpx.ConnectError("reset"),
            httpx.Response(200, json=fixture("vaa_token_transfer")),
        ]
    )

    assert await fetch() is not None
    assert route.call_count == 2


@respx.mock
async def test_a_body_that_is_not_json_is_refused() -> None:
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(200, text="<html>proxy</html>"))

    with pytest.raises(WormholeApiError, match="not JSON"):
        await fetch()


@respx.mock
async def test_a_body_that_is_not_an_object_is_refused() -> None:
    respx.get(f"{API_URL}{PATH}").mock(return_value=httpx.Response(200, json=[1, 2]))

    with pytest.raises(WormholeApiError, match="not an object"):
        await fetch()


@respx.mock
async def test_a_trailing_slash_in_the_api_url_does_not_double_up() -> None:
    # `//api/...` is a 404 from this API, which would read as "not signed yet" —
    # a misconfiguration that looks exactly like a healthy young transfer.
    route = respx.get(f"{API_URL}{PATH}").mock(
        return_value=httpx.Response(200, json=fixture("vaa_token_transfer"))
    )

    await WormholescanClient(api_url=API_URL + "/", sleep=_no_sleep).fetch_vaa(
        emitter_chain=1, emitter_address=EMITTER, sequence=SEQUENCE
    )

    assert route.call_count == 1
