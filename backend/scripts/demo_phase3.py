"""Phase 3 demo: the Lithic adapter against the real sandbox.

    python scripts/demo_phase3.py

Needs `LITHIC_API_KEY` (see .env.example) plus Postgres and Redis. Everything it
prints is a real call to `sandbox.lithic.com`: a cardholder, a virtual card, freeze
and unfreeze, funding twice under one ref, a balance, a simulated authorization and
its clearing — and then those genuine provider payloads run through our own webhook
pipeline and land in the ledger.

Two things are worth watching for, because they are the point of the phase:

**Nothing here is Lithic-shaped except the adapter.** `receive()` verifies, dedups,
normalizes, ledgers and dispatches a Lithic delivery with no knowledge of Lithic. The
only reason this provider exists to the rest of the service is one `register()` line
in `app/issuers/__init__.py`.

**The last step replays real payloads, it does not receive them.** A genuine inbound
delivery needs an event subscription pointing at a public URL. Instead this reads the
events Lithic actually recorded for the calls above (`GET /v1/events`, whose `payload`
is byte-for-byte what a webhook carries), signs each one with a locally chosen secret,
and pushes it through the receiver. The signature check is therefore ours-to-ours; the
*payloads* are the provider's.

The script closes the card it created on the way out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.db import get_sessionmaker
from app.core.money import Money
from app.core.redis import get_redis_client
from app.issuers import registry
from app.issuers.base import CreateCardholderRequest, CreateCardRequest
from app.issuers.lithic import LithicAdapter
from app.issuers.lithic.client import LithicClient
from app.issuers.lithic.config import get_lithic_settings
from app.issuers.lithic.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WEBHOOK_ID_HEADER,
    sign,
)
from app.webhooks.receiver import receive

PROVIDER = "lithic"

#: Used only to sign the replayed payloads below, so verification has something real
#: to check. The genuine per-program secret comes from
#: `GET /v1/event_subscriptions/{token}/secret` and is not needed for this demo.
DEMO_SECRET = "whsec_ZGVtby1vbmx5LW5vdC1hLXJlYWwtc2VjcmV0MTI="

#: How long to wait for the sandbox to reflect a simulated clearing.
SETTLE_ATTEMPTS = 12
SETTLE_PAUSE_SECONDS = 3.0


def heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def wait_for(client: LithicClient, token: str, wanted: str) -> dict[str, Any]:
    """Poll one transaction until the sandbox reports `wanted`. It is asynchronous."""
    for _ in range(SETTLE_ATTEMPTS):
        try:
            transaction = await client.get(f"/transactions/{token}")
        except Exception:
            transaction = {}
        if transaction.get("status") == wanted:
            return transaction
        await asyncio.sleep(SETTLE_PAUSE_SECONDS)
    print(f"  (gave up waiting for {wanted}; the sandbox is slow today)")
    return {}


async def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    settings = get_lithic_settings()
    if not settings.api_key:
        print("LITHIC_API_KEY is not set — see .env.example", file=sys.stderr)
        return 2

    client = LithicClient(
        base_url=settings.api_base_url,
        api_key=settings.api_key,
        timeout=settings.request_timeout_seconds,
    )
    # Registered with a known webhook secret so the replay below can be verified.
    adapter = LithicAdapter(client=client, webhook_secret=DEMO_SECRET)
    registry.register(PROVIDER, lambda: adapter, replace=True)

    heading("registered issuers")
    for provider_id, funding_model in registry.describe():
        print(f"  {provider_id:<20} {funding_model}")

    heading("a cardholder and a card at a fiat-rail issuer")
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(
            email="ada@example.test",
            first_name="Ada",
            last_name="Lovelace",
            external_ref=f"demo-{datetime.now(UTC):%Y%m%d%H%M%S}",
        )
    )
    card = await adapter.create_card(
        holder.cardholder_id,
        CreateCardRequest(currency="USD", spend_limit_minor=50_000, memo="StableCard Rail demo"),
    )
    print(f"  account         {holder.cardholder_id}  ({holder.state})")
    expiry = f"{card.exp_month:02d}/{card.exp_year}"
    print(f"  card            {card.card_id} ****{card.last_four}  exp {expiry}")
    print(f"  state           {card.state}   (Lithic virtual cards are live on creation)")
    print(f"  deposit address {card.deposit_address}   (a fiat rail has none)")
    print(f"  balance         {await adapter.get_balance(card.card_id)}")

    heading("freeze, then unfreeze")
    print(f"  freeze          {(await adapter.freeze_card(card.card_id)).state}")
    print(f"  activate        {(await adapter.activate_card(card.card_id)).state}")

    heading("funding is idempotent under one funding_ref")
    first = await adapter.fund_card(card.card_id, Money(25_000, "USD"), "intent-demo-1")
    second = await adapter.fund_card(card.card_id, Money(25_000, "USD"), "intent-demo-1")
    print(f"  first call      {first.status}  issuer_ref={first.issuer_funding_ref}")
    print(f"  replayed call   {second.status}  replayed={second.raw['replayed']}")
    before, after = first.raw["spend_limit_before"], first.raw["spend_limit_after"]
    print(f"  spend limit     {before} -> {after}")
    print(f"  balance         {await adapter.get_balance(card.card_id)}  (credited once)")

    heading("a simulated authorization, and its clearing")
    # The PAN is deliberately not on the `Card` DTO (SPEC.md §9.2), so the demo reads
    # it from the provider directly to drive their simulator.
    pan = (await client.get(f"/cards/{card.card_id}"))["pan"]
    authorized = await client.post(
        "/simulate/authorize",
        json_body={
            "pan": pan,
            "amount": 1_234,
            "descriptor": "COFFEE BAR",
            "merchant_acceptor_id": "OODKZAPJVN4YS7O",
        },
    )
    print(f"  authorization   {authorized['token']}")
    await wait_for(client, authorized["token"], "PENDING")
    print(f"  balance         {await adapter.get_balance(card.card_id)}  (the hold is counted)")
    await client.post(
        "/simulate/clearing", json_body={"token": authorized["token"], "amount": 1_234}
    )
    await wait_for(client, authorized["token"], "SETTLED")
    print(f"  cleared         balance {await adapter.get_balance(card.card_id)}")

    heading("Lithic's own event payloads through our pipeline")
    events = await client.get("/events", params={"page_size": 10})
    mine = [
        event
        for event in events["data"]
        if event["payload"].get("card_token") == card.card_id
        or event["payload"].get("account_token") == holder.cardholder_id
    ]
    if not mine:
        print("  (no events recorded yet; they lag the calls by a few seconds)")

    sessionmaker = get_sessionmaker()
    redis = get_redis_client()
    async with sessionmaker() as session:
        for event in reversed(mine):
            body = json.dumps(event["payload"], separators=(",", ":")).encode()
            timestamp = str(int(datetime.now(UTC).timestamp()))
            headers = {
                WEBHOOK_ID_HEADER: event["token"],
                TIMESTAMP_HEADER: timestamp,
                SIGNATURE_HEADER: sign(
                    DEMO_SECRET, webhook_id=event["token"], timestamp=timestamp, body=body
                ),
            }
            outcome = await receive(
                session, redis, provider_id=PROVIDER, headers=headers, body=body
            )
            label = event["payload"]["event_type"]
            print(f"  {label:<28} -> {outcome.event_type:<22} ledger #{outcome.ledger_event_id}")

        if mine:
            heading("the same delivery again")
            newest = mine[0]
            body = json.dumps(newest["payload"], separators=(",", ":")).encode()
            timestamp = str(int(datetime.now(UTC).timestamp()))
            repeat = await receive(
                session,
                redis,
                provider_id=PROVIDER,
                headers={
                    WEBHOOK_ID_HEADER: newest["token"],
                    TIMESTAMP_HEADER: timestamp,
                    SIGNATURE_HEADER: sign(
                        DEMO_SECRET, webhook_id=newest["token"], timestamp=timestamp, body=body
                    ),
                },
                body=body,
            )
            print(
                f"  duplicate       {repeat.duplicate}  (Lithic retries keep the same webhook-id)"
            )
            print(f"  points at row   #{repeat.ledger_event_id}  (nothing new written)")

    heading("cleaning up")
    closed = await adapter.cancel_card(card.card_id)
    print(f"  card            {closed.card_id} -> {closed.state}")
    print("  the account holder stays: Lithic has no delete, and it costs nothing.")
    await redis.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
