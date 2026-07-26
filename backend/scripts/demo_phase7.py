"""Phase 7 demo: a 3DS challenge becomes a code, and comes back as an answer.

    python scripts/demo_phase7.py                  # the whole §6 flow, no network
    python scripts/demo_phase7.py --decline        # the other answer
    python scripts/demo_phase7.py --unsupported    # a provider with nowhere to send it
    python scripts/demo_phase7.py --expired        # a challenge that arrives dead
    python scripts/demo_phase7.py --leave-open     # stop before answering, for the API/socket

SPEC.md §6, end to end, against a real database and a real Redis:

    signed webhook -> verify/dedup/ledger/dispatch -> OTP consumer
                   -> Redis (short TTL) -> poll + WebSocket push
                   -> approve/decline -> back through the adapter

**Needs no credentials and no network.** The provider is `gnosis_pay_mock`, whose
simulator signs its own deliveries and holds its own challenge state — which is what
SPEC.md §6.1 has in mind by "or the mock adapter's simulator". Nothing here is a
stand-in for the pipeline itself: the receiver, the consumer, the store, the push
channel and the response path are the same code the running service uses.

Two things worth watching for, because they are the phase's findings made visible:

**The ledger never holds the code.** The challenge arrives with `otpCode` in its body
and the ledger row shows `[redacted]`. `CardEvent.otp_code` is excluded from every
serializer, so the code exists in memory and in Redis under a TTL, and nowhere else
(docs/ARCHITECTURE.md §11.2). This script prints both so the difference is visible.

**The push channel keeps nothing.** A subscriber that is listening sees the challenge
the moment it is stored; one that connects afterwards gets it from the snapshot the
socket sends on connect, not from a replay. Both are shown.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.time import utcnow
from app.issuers import registry
from app.issuers.base import (
    ChallengeDecision,
    ChallengeResponseUnsupported,
    CreateCardholderRequest,
    CreateCardRequest,
)
from app.issuers.gnosis_pay_mock import GnosisPayMockAdapter
from app.ledger.models import LedgerEvent
from app.otp.push import subscription
from app.otp.service import answer_challenge, deliver_challenge
from app.otp.store import OtpChallenge, OtpStore, Remembered
from app.webhooks.receiver import receive

PROVIDER = "gnosis_pay_mock"

#: Long enough that nothing here races the deadline, short enough to be plainly a
#: challenge TTL rather than a session.
CHALLENGE_TTL_SECONDS = 300


def heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def make_card(adapter: GnosisPayMockAdapter) -> str:
    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="ada@example.test", first_name="Ada", last_name="Lovelace")
    )
    card = await adapter.create_card(holder.cardholder_id, CreateCardRequest())
    await adapter.activate_card(card.card_id)
    return card.card_id


async def clear_previous_run(redis: Redis, store: OtpStore) -> None:
    """Forget anything a previous run left open.

    Not a reset of the world — the ledger is append-only and stays — just the open
    challenges, so `GET /otp/pending` in this run means this run.
    """
    for challenge in await store.pending(now=utcnow(), limit=100):
        await store.forget(challenge.provider_id, challenge.challenge_id)


async def ledger_rows(
    session: AsyncSession, prefix: str, *, challenge_id: str | None = None
) -> list[LedgerEvent]:
    """Rows of one kind, optionally narrowed to one challenge.

    The narrowing matters because the ledger is append-only and shared between runs:
    without it, this run's report includes every previous run's rows.
    """
    session.expire_all()
    result = await session.execute(select(LedgerEvent).order_by(LedgerEvent.id))
    return [
        row
        for row in result.scalars().all()
        if row.event_type.startswith(prefix)
        and (challenge_id is None or row.payload.get("challenge_id") == challenge_id)
    ]


def show_ledger(rows: list[LedgerEvent], code: str) -> None:
    for row in rows:
        print(f"  {row.event_type:<24} {json.dumps(_summarize(row.payload), sort_keys=True)}")
    blob = json.dumps([row.payload for row in rows])
    verdict = "absent" if code not in blob else "PRESENT — this is a leak"
    print(f"\n  the code {code!r} in {len(rows)} ledger row(s): {verdict}")


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    """The fields worth reading at a glance, including the redaction marker."""
    kept: dict[str, Any] = {
        key: payload[key]
        for key in ("challenge_id", "derived", "decision", "delivered", "reason")
        if key in payload
    }
    raw = payload.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        kept["otpCode_in_raw"] = raw["data"].get("otpCode")
    return kept


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    print(f"database  {settings.database_url.rsplit('@', 1)[-1]}")
    print(f"redis     {settings.redis_url}")
    print(f"provider  {PROVIDER} (its simulator signs its own deliveries)")

    adapter = registry.get_adapter(PROVIDER)
    assert isinstance(adapter, GnosisPayMockAdapter)
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    store = OtpStore(redis)
    sessionmaker = get_sessionmaker()

    try:
        await clear_previous_run(redis, store)

        heading("1. a card, and a 3DS challenge for it")
        card_id = await make_card(adapter)
        # The simulator numbers from 1 on every start; the ledger does not forget.
        # Without this the second run re-issues `3ds_000001` and reports the first
        # run's rows as its own — see `seed_sequence`.
        async with sessionmaker() as session:
            adapter.simulator.seed_sequence("3ds", len(await ledger_rows(session, "otp.")) + 1)
        ttl = -1 if args.expired else CHALLENGE_TTL_SECONDS
        delivery = adapter.simulator.emit_three_ds_challenge(
            card_id, code=args.code, ttl_seconds=ttl
        )
        body = json.loads(delivery.body)
        print(f"  card              {card_id}")
        print(f"  provider event    {delivery.event_type}")
        print(f"  code in the body  {body['data']['otpCode']}")
        print(f"  expires at        {body['data']['expiresAt']}")
        if args.expired:
            print("  (deliberately already expired — a retry drained long after the fact)")

        heading("2. the webhook pipeline: verify, dedup, ledger, dispatch")
        async with sessionmaker() as session:
            outcome = await receive(
                session,
                redis,
                provider_id=PROVIDER,
                headers=delivery.headers,
                body=delivery.body,
            )
        print(f"  normalized as     {outcome.event_type}")
        print(f"  ledger row        {outcome.ledger_event_id}")
        print(f"  handlers ran      {outcome.handlers_ran or '(none — see below)'}")

        # The receiver dispatches to whatever `create_app()` subscribed. This script
        # is not the app, so the consumer is driven explicitly — same function, same
        # arguments as the registered handler passes.
        async with sessionmaker() as session:
            event = await adapter.parse_webhook(delivery.headers, delivery.body)
            listener = subscription(redis, card_id=card_id)
            async with listener as pubsub:
                result = await deliver_challenge(session, redis, event, now=utcnow())
                pushed = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)

        challenge_id = result.challenge.challenge_id

        heading("3. the OTP consumer")
        print(f"  outcome           {result.outcome.value}")
        print(f"  challenge         {result.challenge.challenge_id}")
        print(f"  code              {result.challenge.code}")
        print(f"  derived by us     {result.challenge.derived}")
        if result.outcome is Remembered.EXPIRED:
            async with sessionmaker() as session:
                show_ledger(
                    await ledger_rows(session, "otp.", challenge_id=challenge_id), args.code
                )
            print(
                "\n  Nothing to deliver: the challenge was dead on arrival, so there is no\n"
                "  code and the ledger says why. That is the whole path for this mode."
            )
            return 0

        heading("4. delivery: push, then poll")
        if pushed is None:
            print("  pushed            (nothing — no listener was connected)")
        else:
            print(f"  pushed            {OtpChallenge.model_validate_json(pushed['data']).code}")
        pending = await store.pending(now=utcnow(), card_id=card_id)
        for item in pending:
            print(
                f"  pending           {item.challenge_id} code={item.code} "
                f"{item.seconds_left(utcnow())}s left"
            )
        print("\n  Both paths carry the same challenge. Polling is the contract and the")
        print("  push is a courtesy — a client that misses the message finds it here.")

        heading("5. what the ledger kept")
        async with sessionmaker() as session:
            show_ledger(
                await ledger_rows(session, "provider.three_ds", challenge_id=challenge_id),
                args.code,
            )
            show_ledger(await ledger_rows(session, "otp.", challenge_id=challenge_id), args.code)

        if args.leave_open:
            heading("6. left open, for the HTTP and WebSocket surfaces")
            print(f"  challenge         {challenge_id} (provider {PROVIDER})")
            print(f"  code              {result.challenge.code}")
            print("\n  With `uvicorn app.main:app --port 8000` running against the same Redis:")
            print("    curl -s localhost:8000/otp/pending | jq")
            print(f"    curl -s -X POST localhost:8000/otp/{PROVIDER}/{challenge_id}/respond \\")
            print("         -H 'content-type: application/json' -d '{\"decision\":\"approve\"}'")
            print("    npx wscat -c 'ws://localhost:8000/ws/otp'")
            return 0

        heading("6. the cardholder answers")
        decision = ChallengeDecision.DECLINE if args.decline else ChallengeDecision.APPROVE
        if args.unsupported:
            # The other half of SPEC.md §6.5. Re-registering the challenge under a
            # provider with no response endpoint is the only way to show it without
            # a second adapter: the mock has one, and Stripe — which does not — never
            # emits a challenge to begin with (ARCHITECTURE §8.8).
            print("  posing as a provider with no challenge-response endpoint")
            unsupported = _UnsupportedProvider()
            registry.register(unsupported.provider_id, lambda: unsupported, replace=True)
            # Moved rather than copied: leaving the original open would make step 7
            # report a challenge that is still pending, which is true of the copy's
            # sibling and misleading about what answering did.
            await store.forget(PROVIDER, challenge_id)
            await store.remember(
                result.challenge.model_copy(update={"provider_id": unsupported.provider_id}),
                now=utcnow(),
            )
            provider_id = unsupported.provider_id
        else:
            provider_id = PROVIDER

        async with sessionmaker() as session:
            stored = await store.get(provider_id, result.challenge.challenge_id)
            assert stored is not None
            answer = await answer_challenge(session, redis, stored, decision, now=utcnow())
        print(f"  decision          {answer.decision.value}")
        print(f"  delivered         {answer.delivered}")
        if answer.detail:
            print(f"  detail            {answer.detail}")
        if not args.unsupported:
            answered = adapter.simulator.get_challenge(result.challenge.challenge_id)
            assert answered is not None
            print(f"  provider records  {answered.answer} at {answered.answered_at}")

        heading("7. the challenge is single-use")
        print(f"  still stored      {await store.get(provider_id, challenge_id)}")
        still_open = await store.pending(now=utcnow(), card_id=card_id)
        print(f"  still pending     {[item.challenge_id for item in still_open] or '(nothing)'}")
        async with sessionmaker() as session:
            show_ledger(await ledger_rows(session, "otp.", challenge_id=challenge_id), args.code)

        print("\nWhat this showed:")
        print("  - a signed challenge webhook became a code the app can read;")
        print("  - the code reached Redis under a TTL, and the ledger never held it;")
        print("  - it arrived by push and by poll, carrying the same shape;")
        if answer.delivered:
            print("  - the decision went back through the adapter to the provider;")
        else:
            print("  - the decision was ledgered with what would have been sent (§6.5);")
        print("  - answering consumed it, so it can neither be shown nor answered again.")
        return 0
    finally:
        await redis.aclose()


class _UnsupportedProvider(GnosisPayMockAdapter):
    """The mock, minus the one method — for the §6.5 fallback path.

    A subclass rather than a second adapter package, because nothing is being
    demonstrated about *this* provider: what is being shown is what the OTP service
    does when `respond_to_challenge` raises, which is the position Stripe Issuing is
    permanently in (ARCHITECTURE §8.8).
    """

    provider_id = "provider_without_3ds_response"

    async def respond_to_challenge(self, challenge_id: str, decision: ChallengeDecision) -> Any:
        raise ChallengeResponseUnsupported(self.provider_id)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decline", action="store_true", help="decline instead of approving")
    parser.add_argument(
        "--unsupported",
        action="store_true",
        help="answer through a provider with no challenge-response endpoint (SPEC.md §6.5)",
    )
    parser.add_argument(
        "--expired", action="store_true", help="deliver a challenge that has already expired"
    )
    parser.add_argument(
        "--leave-open",
        action="store_true",
        help="stop before answering, so the HTTP and WebSocket surfaces have something to show",
    )
    parser.add_argument("--code", default="424242", help="the code the provider sends")
    parser.add_argument("--verbose", action="store_true", help="show application logs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    # Same reason as phase 6's demo: Python buffers stdout when it is a pipe, and
    # this script's output is the whole point of running it.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    try:
        return await run(args)
    except Exception as exc:
        print(f"\nfailed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "\nIs postgres up and migrated, and redis reachable?\n"
            "  docker compose up -d postgres redis\n"
            "  alembic upgrade head",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
