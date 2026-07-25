"""Phase 4 demo: the Stripe Issuing adapter against a real test-mode account.

    python scripts/demo_phase4.py

Needs `STRIPE_ISSUING_API_KEY` (see .env.example), Issuing activated on the account,
test funds in the Issuing balance, plus Postgres and Redis. Everything it prints is a
real call to `api.stripe.com` in test mode: a cardholder, a virtual card, activate,
freeze and unfreeze, funding twice under one ref, a balance, a simulated authorization
and its capture — and then Stripe's own event objects run through our webhook pipeline
and land in the ledger.

Three things are worth watching for, because they are the point of the phase:

**Nothing here is Stripe-shaped except the adapter.** `receive()` verifies, dedups,
normalizes, ledgers and dispatches a Stripe delivery with no knowledge of Stripe. The
only reason this provider exists to the rest of the service is one `register()` line
in `app/issuers/__init__.py` — the whole claim SPEC.md §12.4 asks phase 4 to test.

**Run it next to `demo_phase3.py`.** The two providers are asked the same questions in
the same order through the same interface, and answer with the same vocabulary while
doing entirely different things underneath: Lithic raises a spend limit and records the
funding ref in a card memo, Stripe raises a spending control and records it in
metadata. That side-by-side is the demonstration, more than either script alone.

**The last step replays real event objects, it does not receive them.** A genuine
inbound delivery needs an endpoint secret and a public URL (`stripe listen`, or a
tunnel). Instead this reads the events Stripe actually recorded for the calls above
(`GET /v1/events`, whose objects are byte-for-byte what a webhook carries), signs each
one with a locally chosen secret, and pushes it through the receiver. The signature
check is therefore ours-to-ours; the *payloads* are Stripe's. Which also means this
demo does not settle the one open question in docs/ARCHITECTURE.md §8.2 — whether
Stripe keys its HMAC on the `whsec_` prefix — because it never verifies a signature
Stripe produced.

The script cancels the card it created on the way out. Cardholders stay: Stripe has no
delete for them, and they cost nothing.
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
from app.issuers.stripe_issuing import StripeIssuingAdapter
from app.issuers.stripe_issuing.client import StripeApiError, StripeClient
from app.issuers.stripe_issuing.config import get_stripe_issuing_settings
from app.issuers.stripe_issuing.signing import SIGNATURE_HEADER, signature_header
from app.webhooks.receiver import receive

PROVIDER = "stripe_issuing"

#: Used only to sign the replayed event objects below, so verification has something
#: real to check. The genuine endpoint secret comes from the Dashboard or
#: `stripe listen`, and is not needed for this demo.
DEMO_SECRET = "whsec_ZGVtby1vbmx5LW5vdC1hLXJlYWwtc2VjcmV0MTI="

INITIAL_LIMIT_MINOR = 50_000
FUNDING_MINOR = 25_000
PURCHASE_MINOR = 1_234


def heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def preflight(client: StripeClient) -> str | None:
    """Whether the account can answer at all, or a sentence saying why not.

    Three prerequisites with three different fixes; "invalid request" on its own would
    send whoever runs this looking in the wrong place.
    """
    try:
        balance = await client.get("/balance")
    except (StripeApiError, ValueError) as exc:
        return f"the API key does not work: {exc}"
    if balance.get("livemode") is not False:
        return "that is a live-mode key, and this project is sandbox-only (SPEC.md §2)"

    try:
        await client.get("/issuing/cards", params={"limit": 1})
    except StripeApiError as exc:
        return (
            f"Issuing is not activated on this account ({exc.message}).\n"
            "  Activate it at https://dashboard.stripe.com/issuing/overview, in test mode."
        )

    issuing = balance.get("issuing")
    available = 0
    if isinstance(issuing, dict) and isinstance(issuing.get("available"), list):
        available = sum(
            entry.get("amount", 0)
            for entry in issuing["available"]
            if isinstance(entry, dict) and entry.get("currency") == "usd"
        )
    if available <= 0:
        return (
            "the Issuing balance is empty, so every simulated authorization would "
            "decline.\n  Add test funds at "
            "https://dashboard.stripe.com/test/issuing/balance/overview."
        )
    print(f"  issuing balance {available} minor units available")
    return None


async def simulate_purchase(client: StripeClient, card_id: str) -> dict[str, Any]:
    """One test-helper authorization. Stripe's helpers are synchronous, unlike Lithic's."""
    return await client.post(
        "/test_helpers/issuing/authorizations",
        body={
            "card": card_id,
            "amount": PURCHASE_MINOR,
            "currency": "usd",
            "merchant_data": {
                "name": "COFFEE BY THE PARK",
                "category": "eating_places_restaurants",
                "city": "NEW YORK",
                "country": "US",
            },
        },
    )


async def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    settings = get_stripe_issuing_settings()
    if not settings.api_key:
        print("STRIPE_ISSUING_API_KEY is not set — see .env.example", file=sys.stderr)
        return 2

    client = StripeClient(
        base_url=settings.api_base_url,
        api_key=settings.api_key,
        timeout=settings.request_timeout_seconds,
        api_version=settings.api_version,
    )

    heading("preflight")
    blocked = await preflight(client)
    if blocked is not None:
        print(f"  cannot run: {blocked}", file=sys.stderr)
        return 2
    print("  ok: test-mode key, Issuing activated, balance funded")

    # Registered with a known webhook secret so the replay below can be verified.
    adapter = StripeIssuingAdapter(client=client, webhook_secret=DEMO_SECRET)
    registry.register(PROVIDER, lambda: adapter, replace=True)

    heading("registered issuers")
    for provider_id, funding_model in registry.describe():
        print(f"  {provider_id:<20} {funding_model}")
    print("  three providers, two funding models, and no module outside issuers/ knows")

    heading("a cardholder and a card at the second fiat-rail issuer")
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
        CreateCardRequest(
            currency="USD", spend_limit_minor=INITIAL_LIMIT_MINOR, memo="StableCard Rail demo"
        ),
    )
    print(f"  cardholder      {holder.cardholder_id}  ({holder.state})")
    expiry = f"{card.exp_month:02d}/{card.exp_year}"
    print(f"  card            {card.card_id} ****{card.last_four}  exp {expiry}")
    print(f"  state           {card.state}   (Stripe creates cards inactive)")
    print(f"  deposit address {card.deposit_address}   (a fiat rail has none)")

    heading("activate, freeze, unfreeze")
    # The interesting one: Stripe has no `frozen`, only `inactive`, which also means
    # "never activated". The adapter keeps the missing bit in the card's own metadata,
    # which is why the freeze below does not read back as `unactivated`
    # (docs/ARCHITECTURE.md §8.3).
    print(f"  activate        {(await adapter.activate_card(card.card_id)).state}")
    frozen = await adapter.freeze_card(card.card_id)
    print(f"  freeze          {frozen.state}   (provider status: {frozen.raw['provider_status']})")
    print(f"  activate        {(await adapter.activate_card(card.card_id)).state}")
    print(f"  balance         {await adapter.get_balance(card.card_id)}")

    heading("funding is idempotent under one funding_ref")
    first = await adapter.fund_card(card.card_id, Money(FUNDING_MINOR, "USD"), "intent-demo-1")
    second = await adapter.fund_card(card.card_id, Money(FUNDING_MINOR, "USD"), "intent-demo-1")
    print(f"  first call      {first.status}  issuer_ref={first.issuer_funding_ref}")
    print(f"  replayed call   {second.status}  replayed={second.raw['replayed']}")
    before, after = first.raw["spending_limit_before"], first.raw["spending_limit_after"]
    print(f"  spending limit  {before} -> {after}   (interval all_time)")
    print(f"  balance         {await adapter.get_balance(card.card_id)}  (credited once)")

    heading("a simulated authorization, and its capture")
    authorization = await simulate_purchase(client, card.card_id)
    print(f"  authorization   {authorization['id']}  status={authorization.get('status')}")
    print(f"  balance         {await adapter.get_balance(card.card_id)}  (the hold is counted)")
    captured = await client.post(
        f"/test_helpers/issuing/authorizations/{authorization['id']}/capture"
    )
    print(f"  captured        status={captured.get('status')}")
    # Deliberately the same figure as the line above: the hold became a transaction of
    # the same magnitude. An unchanged balance here is the whole double-counting
    # question answered (docs/ARCHITECTURE.md §8.4, §8.7).
    print(f"  balance         {await adapter.get_balance(card.card_id)}  (unchanged: the")
    print("                  hold became a transaction, and neither is counted twice)")

    await replay_events(client, adapter, card.card_id, holder.cardholder_id)

    heading("cleaning up")
    canceled = await adapter.cancel_card(card.card_id)
    print(f"  card            {canceled.card_id} -> {canceled.state}")
    print("  the cardholder stays: Stripe has no delete, and it costs nothing.")
    return 0


async def replay_events(
    client: StripeClient, adapter: StripeIssuingAdapter, card_id: str, holder_id: str
) -> None:
    """Stripe's own event objects through our pipeline.

    The body of a Stripe webhook *is* an Event object, so these are genuine delivery
    bodies — which is what makes this a test of `parse_webhook` rather than of a mock.
    """
    heading("Stripe's own event objects through our pipeline")
    feed = await client.get("/events", params={"limit": 30})
    events = [event for event in feed.get("data", []) if _is_ours(event, card_id, holder_id)]
    if not events:
        print("  (no events recorded yet; they lag the calls by a few seconds)")
        return

    sessionmaker = get_sessionmaker()
    redis = get_redis_client()
    try:
        async with sessionmaker() as session:
            for event in reversed(events):
                outcome = await receive(session, redis, provider_id=PROVIDER, **_delivery(event))
                print(
                    f"  {event['type']:<34} -> {outcome.event_type:<22} "
                    f"ledger #{outcome.ledger_event_id}"
                )

            heading("the same delivery again")
            repeat = await receive(session, redis, provider_id=PROVIDER, **_delivery(events[0]))
            print(f"  duplicate       {repeat.duplicate}  (dedup is on the evt_ id in the body)")
            print(f"  points at row   #{repeat.ledger_event_id}  (nothing new written)")
    finally:
        await redis.aclose()


def _is_ours(event: Any, card_id: str, holder_id: str) -> bool:
    """Whether one event concerns the card or cardholder this run created."""
    if not isinstance(event, dict):
        return False
    obj = event.get("data", {}).get("object")
    if not isinstance(obj, dict):
        return False
    return card_id in json.dumps(obj) or holder_id in json.dumps(obj)


def _delivery(event: dict[str, Any]) -> dict[str, Any]:
    """One event object as a signed delivery: raw bytes plus the header over them."""
    # Separators matter: the signature covers bytes, and re-serializing differently
    # would be a different document (which is exactly why the receiver keeps the raw
    # body rather than a parsed one).
    body = json.dumps(event, separators=(",", ":")).encode()
    timestamp = str(int(datetime.now(UTC).timestamp()))
    return {
        "headers": {
            SIGNATURE_HEADER: signature_header(DEMO_SECRET, timestamp=timestamp, body=body)
        },
        "body": body,
    }


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
