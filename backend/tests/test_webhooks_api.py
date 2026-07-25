"""`POST /webhooks/{provider_id}` over HTTP (SPEC.md §4).

The receiver's behaviour is covered in `test_webhook_receiver.py`; what is asserted
here is the HTTP contract a provider actually sees — above all that **everything
past verification is a 200**. A provider that gets 5xx retries, backs off, and
eventually stops delivering; a handler of ours being broken must never cause that.
"""

from __future__ import annotations

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.issuers.base import CardEvent, CardEventType
from app.issuers.evm_deposit_mock import Delivery, EvmDepositMockAdapter
from app.webhooks import dispatch
from app.webhooks.retry import RetryQueue
from tests.support import all_ledger_events, make_mock_card

PROVIDER = "evm_deposit_mock"


async def post(client: httpx.AsyncClient, delivery: Delivery) -> httpx.Response:
    return await client.post(
        f"/webhooks/{PROVIDER}", content=delivery.body, headers=delivery.headers
    )


async def an_authorization(adapter: EvmDepositMockAdapter) -> Delivery:
    card_id = await make_mock_card(adapter)
    return adapter.simulator.emit_authorization(card_id, Money(1299, "USD"))


async def test_a_signed_delivery_is_accepted(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter, session: AsyncSession
) -> None:
    delivery = await an_authorization(mock_adapter)

    response = await post(client, delivery)

    assert response.status_code == 200
    assert response.json() == {
        "received": True,
        "duplicate": False,
        "provider_id": PROVIDER,
        "event_id": delivery.event_id,
        "event_type": "authorization",
        "ledger_event_id": (await all_ledger_events(session))[0].id,
        "handlers_failed": [],
    }


async def test_a_duplicate_delivery_is_also_a_200(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter, session: AsyncSession
) -> None:
    delivery = await an_authorization(mock_adapter)
    await post(client, delivery)

    response = await post(client, delivery)

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert len(await all_ledger_events(session)) == 1


async def test_an_unsigned_delivery_is_a_401(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter, session: AsyncSession
) -> None:
    delivery = await an_authorization(mock_adapter)

    response = await client.post(f"/webhooks/{PROVIDER}", content=delivery.body)

    assert response.status_code == 401
    assert response.json()["code"] == "signature_rejected"
    # The reason is not disclosed, and nothing is recorded.
    assert "timestamp" not in response.text
    assert await all_ledger_events(session) == []


async def test_a_tampered_body_is_a_401(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter
) -> None:
    delivery = await an_authorization(mock_adapter)
    response = await client.post(
        f"/webhooks/{PROVIDER}",
        content=delivery.body.replace(b"1299", b"999999"),
        headers=delivery.headers,
    )
    assert response.status_code == 401


async def test_an_unknown_provider_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/webhooks/wells_fargo", content=b"{}")
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_provider"


async def test_a_failing_handler_still_returns_200_and_queues_a_retry(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter, redis_client: Redis
) -> None:
    """SPEC.md §4: handler exceptions never cause a non-2xx after verification."""

    async def boom(event: CardEvent) -> None:
        raise RuntimeError("downstream is down")

    dispatch.subscribe(CardEventType.AUTHORIZATION, "boom", boom)
    delivery = await an_authorization(mock_adapter)

    response = await post(client, delivery)

    assert response.status_code == 200
    assert response.json()["handlers_failed"] == ["boom"]
    assert await RetryQueue(redis_client).size() == 1


async def test_an_unparseable_body_is_accepted_as_unmapped(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter, session: AsyncSession
) -> None:
    delivery = mock_adapter.simulator.emit_unknown("card.who_knows", {"a": 1})

    response = await post(client, delivery)

    assert response.status_code == 200
    assert response.json()["event_type"] == "unmapped"
    assert (await all_ledger_events(session))[0].event_type == "provider.unmapped"


async def test_the_endpoint_reads_raw_bytes_not_parsed_json(
    client: httpx.AsyncClient, mock_adapter: EvmDepositMockAdapter
) -> None:
    # Re-serializing before verifying is the classic way to break a signature
    # check: any reordering or whitespace change would fail here.
    delivery = await an_authorization(mock_adapter)
    reserialized = delivery.body.replace(b",", b", ")
    assert reserialized != delivery.body

    assert (await post(client, delivery)).status_code == 200
    response = await client.post(
        f"/webhooks/{PROVIDER}", content=reserialized, headers=delivery.headers
    )
    assert response.status_code == 401
