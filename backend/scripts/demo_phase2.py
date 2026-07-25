"""Phase 2 demo: the issuer abstraction and the webhook pipeline.

    python scripts/demo_phase2.py

Runs the real pipeline — verify → dedup → parse → ledger → dispatch — against a
real database and Redis, using the mock provider's own signed deliveries. No
network, no provider account, no fixtures.

What it shows, in order: a card at a `CRYPTO_DEPOSIT` issuer, idempotent funding,
an accepted delivery, the same delivery ignored as a duplicate, a tampered
delivery rejected, an event type we do not model recorded as `unmapped`, and a
failing handler retried and then dead-lettered. It ends with the ledger and a
ready-to-paste `curl` for the HTTP endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.money import Money
from app.core.redis import get_redis_client
from app.issuers import registry
from app.issuers.base import CardEvent, CardEventType, CreateCardholderRequest, CreateCardRequest
from app.issuers.evm_deposit_mock import Delivery, EvmDepositMockAdapter
from app.ledger.models import LedgerEvent
from app.webhooks import dispatch
from app.webhooks.models import WebhookDeadLetter
from app.webhooks.receiver import SignatureRejected, receive
from app.webhooks.retry import RetryQueue

PROVIDER = "evm_deposit_mock"
URL = "http://localhost:8000"


def heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def failing_handler(event: CardEvent) -> None:
    raise RuntimeError("card-balance projection is unavailable")


def as_curl(delivery: Delivery) -> str:
    parts = ["curl", "-i", "-X", "POST", f"{URL}/webhooks/{PROVIDER}"]
    for name, value in delivery.headers.items():
        parts += ["-H", f"{name}: {value}"]
    parts += ["--data-binary", delivery.body.decode()]
    return " ".join(shlex.quote(part) for part in parts)


async def main() -> None:
    # Keep the narrative readable; the ledger dump shows what the logs would.
    logging.basicConfig(level=logging.CRITICAL)
    redis = get_redis_client()
    sessionmaker = get_sessionmaker()

    adapter = registry.get_adapter(PROVIDER)
    assert isinstance(adapter, EvmDepositMockAdapter)
    simulator = adapter.simulator

    heading("registered issuers")
    for provider_id, funding_model in registry.describe():
        print(f"  {provider_id:<20} {funding_model}")

    heading("a card at a crypto-deposit issuer")
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="demo@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(
        holder.cardholder_id, CreateCardRequest(currency="USD", spend_limit_minor=100_000)
    )
    await adapter.activate_card(card.card_id)
    print(f"  cardholder      {holder.cardholder_id}")
    print(f"  card            {card.card_id} ****{card.last_four}")
    print(f"  deposit address {card.deposit_address}")
    print(f"  balance         {await adapter.get_balance(card.card_id)}")

    heading("funding is idempotent under one funding_ref")
    first = await adapter.fund_card(card.card_id, Money(2500, "USD"), "intent-demo-1")
    second = await adapter.fund_card(card.card_id, Money(2500, "USD"), "intent-demo-1")
    print(f"  first call      {first.status} issuer_ref={first.issuer_funding_ref}")
    print(f"  replayed call   {second.status} issuer_ref={second.issuer_funding_ref}")
    print(f"  same result     {first == second}")
    print(f"  balance         {await adapter.get_balance(card.card_id)}  (credited once)")
    simulator.drain_deliveries()  # the provider's settlement confirmation; not our subject here

    async with sessionmaker() as session:
        heading("a signed delivery through the pipeline")
        authorization = simulator.emit_authorization(
            card.card_id, Money(1299, "USD"), merchant="Coffee"
        )
        outcome = await receive(
            session,
            redis,
            provider_id=PROVIDER,
            headers=authorization.headers,
            body=authorization.body,
        )
        print(f"  {authorization.event_type:<32} -> {outcome.event_type}")
        print(f"  ledger row      #{outcome.ledger_event_id}")
        print(f"  bus stream id   {outcome.stream_id}")

        heading("the same delivery again")
        repeat = await receive(
            session,
            redis,
            provider_id=PROVIDER,
            headers=authorization.headers,
            body=authorization.body,
        )
        print(f"  duplicate       {repeat.duplicate}")
        print(f"  points at row   #{repeat.ledger_event_id}  (nothing new written)")

        heading("a tampered delivery")
        try:
            await receive(
                session,
                redis,
                provider_id=PROVIDER,
                headers=authorization.headers,
                body=authorization.body.replace(b"1299", b"999999"),
            )
        except SignatureRejected as exc:
            print(f"  rejected        {exc}")
            print("  recorded        nothing — the ledger only holds authenticated events")

        heading("an event type we do not model")
        unknown = simulator.emit_unknown("card.quantum_entangled", {"card_id": card.card_id})
        unmapped = await receive(
            session, redis, provider_id=PROVIDER, headers=unknown.headers, body=unknown.body
        )
        print(f"  {unknown.event_type:<32} -> {unmapped.event_type}")
        print("  kept            the raw payload, under its own provider label")

        heading("a handler that keeps failing")
        dispatch.subscribe(CardEventType.SETTLEMENT, "balance_projection", failing_handler)
        settlement = simulator.emit_settlement(card.card_id, Money(1299, "USD"))
        failed = await receive(
            session, redis, provider_id=PROVIDER, headers=settlement.headers, body=settlement.body
        )
        print(f"  delivery        accepted anyway (ledger row #{failed.ledger_event_id})")
        print(f"  handlers failed {list(failed.handlers_failed)}")

        queue = RetryQueue(redis)
        now = datetime.now(UTC)
        passes = 0
        while await queue.size():
            now += timedelta(hours=1)  # fast-forward instead of waiting out the backoff
            report = await dispatch.drain_due(session, redis, now=now)
            passes += 1
            print(
                f"  retry pass {passes}    rescheduled={list(report.rescheduled)} "
                f"dead_lettered={list(report.dead_lettered)}"
            )

        letters = (
            (await session.execute(select(WebhookDeadLetter).order_by(WebhookDeadLetter.id)))
            .scalars()
            .all()
        )
        for letter in letters:
            print(
                f"  dead letter     {letter.handler} after {letter.attempts} attempts: "
                f"{letter.last_error}"
            )

        heading("ledger")
        events = (
            (await session.execute(select(LedgerEvent).order_by(LedgerEvent.id))).scalars().all()
        )
        for event in events:
            print(f"  #{event.id:<3} {event.occurred_at:%H:%M:%S} {event.event_type}")

    heading("the same thing over HTTP")
    print("  a freshly signed delivery, ready to paste (the server must be running):\n")
    print(f"  {as_curl(simulator.emit_authorization(card.card_id, Money(499, 'USD')))}")


if __name__ == "__main__":
    asyncio.run(main())
