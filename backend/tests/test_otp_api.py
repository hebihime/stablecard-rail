"""`GET /otp/pending` — the reliable half of OTP delivery (SPEC.md §6.3).

SPEC.md §6.3 puts polling and push in that order deliberately: "polling is the
reliable fallback; push is the demo-quality path". So this endpoint is the
contract. A client that only ever polls must be able to complete a challenge, and
these tests are written from that angle — what the modal in §6.4 needs in order to
render itself is what the response has to contain.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow
from app.issuers import registry
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter
from app.otp.store import OtpChallenge, OtpStore, challenge_key
from tests.support import StubIssuerAdapter, all_ledger_events, make_mock_card

CODE = "918273"


@pytest.fixture
def store(redis_client: Redis) -> OtpStore:
    return OtpStore(redis_client)


async def seed(
    store: OtpStore,
    *,
    challenge_id: str = "3ds_000001",
    provider_id: str = "gnosis_pay_mock",
    card_id: str | None = "card_1",
    code: str = CODE,
    expires_in: int = 300,
    derived: bool = False,
) -> OtpChallenge:
    now = utcnow()
    challenge = OtpChallenge(
        provider_id=provider_id,
        challenge_id=challenge_id,
        card_id=card_id,
        cardholder_id="user_1",
        event_id="evt-1",
        code=code,
        derived=derived,
        delivered_at=now,
        expires_at=now + timedelta(seconds=expires_in),
        amount_minor=1234,
        currency="USD",
    )
    await store.remember(challenge, now=now)
    return challenge


async def test_nothing_pending_is_an_empty_list_not_a_404(client: httpx.AsyncClient) -> None:
    # A client polls on a timer. "No open challenges" is the normal answer and has
    # to be cheap and unremarkable, not an error to be special-cased.
    response = await client.get("/otp/pending")

    assert 200 == response.status_code
    assert {"count": 0, "challenges": []} == response.json()


async def test_a_pending_challenge_carries_what_the_modal_needs(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    await seed(store)

    body = (await client.get("/otp/pending")).json()

    assert 1 == body["count"]
    challenge = body["challenges"][0]
    assert "3ds_000001" == challenge["challenge_id"]
    assert "gnosis_pay_mock" == challenge["provider_id"]
    assert "card_1" == challenge["card_id"]
    # The code itself: SPEC.md §6.4's modal shows it with a copy button, so this
    # endpoint is the one place in the service that deliberately hands it out.
    assert CODE == challenge["code"]
    assert challenge["derived"] is False
    assert 1234 == challenge["amount_minor"]
    assert "USD" == challenge["currency"]


async def test_a_pending_challenge_says_how_long_is_left(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    # For the countdown in §6.4. A deadline alone would make the client subtract
    # against its own clock, which is the one clock we have no control over.
    await seed(store, expires_in=120)

    challenge = (await client.get("/otp/pending")).json()["challenges"][0]

    assert 0 < challenge["seconds_remaining"] <= 120
    assert challenge["expires_at"].startswith("20")


async def test_pending_is_ordered_by_which_challenge_dies_first(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    await seed(store, challenge_id="late", expires_in=300)
    await seed(store, challenge_id="soon", expires_in=30)

    body = (await client.get("/otp/pending")).json()

    assert ["soon", "late"] == [item["challenge_id"] for item in body["challenges"]]


async def test_pending_can_be_narrowed_to_one_card(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    await seed(store, challenge_id="a", card_id="card_1")
    await seed(store, challenge_id="b", card_id="card_2")

    body = (await client.get("/otp/pending", params={"card_id": "card_2"})).json()

    assert ["b"] == [item["challenge_id"] for item in body["challenges"]]


async def test_a_challenge_whose_code_is_gone_is_not_pending(
    client: httpx.AsyncClient, store: OtpStore, redis_client: Redis
) -> None:
    # Redis drops the code on its own and the index entry outlives it, so this is
    # really a test that the endpoint reads the code rather than the index.
    await seed(store)
    await redis_client.delete(challenge_key("gnosis_pay_mock", "3ds_000001"))

    body = (await client.get("/otp/pending")).json()

    assert 0 == body["count"]


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_an_out_of_range_limit_is_refused(client: httpx.AsyncClient, limit: int) -> None:
    response = await client.get("/otp/pending", params={"limit": limit})

    assert 422 == response.status_code


async def test_the_limit_caps_what_is_returned(client: httpx.AsyncClient, store: OtpStore) -> None:
    for index in range(4):
        await seed(store, challenge_id=f"c{index}", expires_in=30 + index)

    body = (await client.get("/otp/pending", params={"limit": 2})).json()

    assert ["c0", "c1"] == [item["challenge_id"] for item in body["challenges"]]


# ------------------------------------------------------- approve / decline ----
# SPEC.md §6.5. Two paths, and the difference is a provider capability rather than
# an error: the mock has a challenge-response endpoint (its simulator is ours), and
# a provider without one has its decision ledgered instead of delivered.


async def test_answering_an_unknown_challenge_is_a_404(client: httpx.AsyncClient) -> None:
    # Never delivered, expired, or already answered — the same fact from the
    # client's side, and none of them is a provider failure.
    response = await client.post(
        "/otp/gnosis_pay_mock/3ds_nope/respond", json={"decision": "approve"}
    )

    assert 404 == response.status_code


@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_a_decision_reaches_the_provider_that_can_take_one(
    client: httpx.AsyncClient,
    redis_client: Redis,
    store: OtpStore,
    mock_adapter: GnosisPayMockAdapter,
    decision: str,
) -> None:
    card_id = await make_mock_card(mock_adapter)
    delivery = mock_adapter.simulator.emit_three_ds_challenge(card_id)
    event = await mock_adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.challenge_id is not None
    await seed(store, challenge_id=event.challenge_id, card_id=card_id)

    response = await client.post(
        f"/otp/gnosis_pay_mock/{event.challenge_id}/respond", json={"decision": decision}
    )

    assert 200 == response.status_code
    body = response.json()
    assert decision == body["decision"]
    assert body["delivered"] is True
    answered = mock_adapter.simulator.get_challenge(event.challenge_id)
    assert answered is not None
    assert decision == answered.answer


async def test_answering_consumes_the_challenge(
    client: httpx.AsyncClient, store: OtpStore, mock_adapter: GnosisPayMockAdapter
) -> None:
    # Single-use: the code is gone from Redis rather than left to expire, so the
    # modal cannot be answered twice and a stale code cannot be shown again.
    card_id = await make_mock_card(mock_adapter)
    delivery = mock_adapter.simulator.emit_three_ds_challenge(card_id)
    event = await mock_adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.challenge_id is not None
    await seed(store, challenge_id=event.challenge_id, card_id=card_id)

    await client.post(
        f"/otp/gnosis_pay_mock/{event.challenge_id}/respond", json={"decision": "approve"}
    )

    assert await store.get("gnosis_pay_mock", event.challenge_id) is None
    assert 0 == (await client.get("/otp/pending")).json()["count"]
    # And a second answer is a 404 rather than a duplicate at the provider.
    again = await client.post(
        f"/otp/gnosis_pay_mock/{event.challenge_id}/respond", json={"decision": "approve"}
    )
    assert 404 == again.status_code


async def test_a_decision_is_ledgered_with_what_would_have_been_sent(
    client: httpx.AsyncClient, session: AsyncSession, store: OtpStore
) -> None:
    """SPEC.md §6.5's fallback, for a provider with nowhere to send a response.

    The stub provider inherits the interface default, which raises
    `ChallengeResponseUnsupported` — the same position Stripe Issuing is in (§8.8).
    The request succeeds and the decision is recorded: a capability gap is not a
    failure the cardholder should be shown.
    """
    stub = StubIssuerAdapter()
    registry.register(stub.provider_id, lambda: stub, replace=True)
    await seed(store, provider_id=stub.provider_id, challenge_id="3ds_stub")

    response = await client.post(
        f"/otp/{stub.provider_id}/3ds_stub/respond", json={"decision": "decline"}
    )

    assert 200 == response.status_code
    body = response.json()
    assert body["delivered"] is False
    assert body["detail"] is not None
    entry = next(
        row for row in await all_ledger_events(session) if row.event_type == "otp.responded"
    )
    assert "decline" == entry.payload["decision"]
    assert entry.payload["delivered"] is False
    assert {"challenge_id": "3ds_stub", "decision": "decline"} == entry.payload["would_send"]
    # And it is still consumed: there is nothing more to wait for.
    assert await store.get(stub.provider_id, "3ds_stub") is None


async def test_a_delivered_decision_is_ledgered_too(
    client: httpx.AsyncClient,
    session: AsyncSession,
    store: OtpStore,
    mock_adapter: GnosisPayMockAdapter,
) -> None:
    card_id = await make_mock_card(mock_adapter)
    delivery = mock_adapter.simulator.emit_three_ds_challenge(card_id)
    event = await mock_adapter.parse_webhook(delivery.headers, delivery.body)
    assert event.challenge_id is not None
    await seed(store, challenge_id=event.challenge_id, card_id=card_id)

    await client.post(
        f"/otp/gnosis_pay_mock/{event.challenge_id}/respond", json={"decision": "approve"}
    )

    entry = next(
        row for row in await all_ledger_events(session) if row.event_type == "otp.responded"
    )
    assert entry.payload["delivered"] is True
    assert event.challenge_id == entry.payload["provider_ref"]
    assert card_id == entry.card_id


async def test_a_provider_that_refuses_the_answer_leaves_the_challenge_alone(
    client: httpx.AsyncClient, store: OtpStore, mock_adapter: GnosisPayMockAdapter
) -> None:
    # The simulator refuses an answer to a challenge it never issued, which is the
    # provider disagreeing with our store. 502, and nothing is consumed — the
    # cardholder can try again rather than losing a live challenge to our bookkeeping.
    await seed(store, challenge_id="3ds_not_at_the_provider")

    response = await client.post(
        "/otp/gnosis_pay_mock/3ds_not_at_the_provider/respond", json={"decision": "approve"}
    )

    assert 502 == response.status_code
    assert await store.get("gnosis_pay_mock", "3ds_not_at_the_provider") is not None


async def test_an_unknown_decision_is_refused(client: httpx.AsyncClient, store: OtpStore) -> None:
    await seed(store)

    response = await client.post(
        "/otp/gnosis_pay_mock/3ds_000001/respond", json={"decision": "maybe"}
    )

    assert 422 == response.status_code


async def test_an_unknown_provider_is_a_404_not_a_500(
    client: httpx.AsyncClient, store: OtpStore
) -> None:
    # The store is keyed on the pair, so a challenge id alone is ambiguous — and a
    # provider nobody registered must not reach `get_adapter` as a 500.
    response = await client.post(
        "/otp/no_such_provider/3ds_1/respond", json={"decision": "approve"}
    )

    assert 404 == response.status_code
