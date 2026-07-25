"""`GET /ledger` — the read surface used by the demo UI and interview walk-throughs."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import Money
from app.funding.machine import advance, create_intent
from app.funding.states import FundingState
from app.ledger.writer import record


async def test_healthz_reports_dependency_connectivity(client: httpx.AsyncClient) -> None:
    # Redis joins the probe in phase 2: a receiver that cannot dedup is worse than
    # one that is down, so "up but no Redis" must not read as healthy.
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_empty_ledger_returns_an_empty_page(client: httpx.AsyncClient) -> None:
    response = await client.get("/ledger")
    assert response.status_code == 200
    assert response.json() == {"count": 0, "events": []}


async def test_events_are_returned_oldest_first_with_full_detail(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    intent = await create_intent(
        session, provider_id="evm_deposit_mock", card_id="card_1", amount=Money(2500, "USD")
    )
    await advance(session, intent.id, FundingState.DEPOSIT_CONFIRMED, reason="deposit finalized")

    response = await client.get("/ledger")
    body = response.json()

    assert body["count"] == 2
    created, transitioned = body["events"]
    assert created["event_type"] == "funding_intent.created"
    assert created["state_before"] is None
    assert created["state_after"] == "PENDING"
    assert created["amount_minor"] == 2500
    assert created["currency"] == "USD"
    assert created["intent_id"] == str(intent.id)
    assert transitioned["state_after"] == "DEPOSIT_CONFIRMED"
    assert transitioned["payload"]["reason"] == "deposit finalized"
    assert created["occurred_at"].endswith("Z") or "+00:00" in created["occurred_at"]


async def test_filter_by_card_id(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await record(session, event_type="a", card_id="card_1")
    await record(session, event_type="b", card_id="card_2")
    await session.commit()

    response = await client.get("/ledger", params={"card_id": "card_1"})
    body = response.json()
    assert body["count"] == 1
    assert body["events"][0]["event_type"] == "a"


async def test_filter_by_intent_id(client: httpx.AsyncClient, session: AsyncSession) -> None:
    intent = await create_intent(
        session, provider_id="evm_deposit_mock", card_id="card_1", amount=Money(100, "USD")
    )
    await record(session, event_type="unrelated", card_id="card_9")
    await session.commit()

    response = await client.get("/ledger", params={"intent_id": str(intent.id)})
    body = response.json()
    assert body["count"] == 1
    assert body["events"][0]["event_type"] == "funding_intent.created"


async def test_filter_by_event_type(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await record(session, event_type="card.authorization", card_id="card_1")
    await record(session, event_type="card.settlement", card_id="card_1")
    await session.commit()

    response = await client.get("/ledger", params={"event_type": "card.settlement"})
    assert response.json()["count"] == 1


async def test_limit_caps_the_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    for index in range(5):
        await record(session, event_type=f"e{index}", card_id="card_1")
    await session.commit()

    response = await client.get("/ledger", params={"limit": 2})
    assert response.json()["count"] == 2


async def test_limit_is_validated(client: httpx.AsyncClient) -> None:
    assert (await client.get("/ledger", params={"limit": 0})).status_code == 422
    assert (await client.get("/ledger", params={"limit": 501})).status_code == 422


async def test_malformed_intent_id_is_a_client_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/ledger", params={"intent_id": "not-a-uuid"})
    assert response.status_code == 422
