"""Turning a challenge webhook into a code the app can show (SPEC.md §6.1, §6.2).

The second consumer on the `EventBus`. Phase 2 built the pipe and registered
nothing (docs/ARCHITECTURE.md §3.10); phase 5 connected funding's settlement
handler; this is OTP's.

**Extract, or derive.** SPEC.md §6.2 says the service "extracts/derives the code",
and both halves of that turned out to be load-bearing once the two providers were
read properly:

* the mock models the **ACS-orchestrated** shape and puts the code in the webhook
  body, so there is one to extract;
* Lithic's is **customer-orchestrated** — "your organization delivers the challenge
  to the cardholder through your chosen channel" — so at the moment their webhook
  arrives no code exists anywhere. Minting one is the protocol, not a stand-in
  (§11.4).

`OtpChallenge.derived` records which happened, because the two are different
objects: one is a value we relay, the other is a value we will have to check.

**The order is store, then ledger, and a retry re-attempts both.** A run that
stores the code and dies before writing its ledger row has to converge, so
`ALREADY_KNOWN` from the store is not treated as "nothing to do" — the row is
attempted again under its own idempotency key. The reverse order would be worse:
a ledger row claiming a code was delivered, and no code.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.money import Money
from app.core.time import utcnow
from app.issuers.base import CardEvent, CardEventType
from app.ledger import event_types
from app.ledger.writer import find_by_idempotency_key, record
from app.otp.store import OtpChallenge, OtpStore, Remembered
from app.webhooks.dispatch import Handler, subscribe

__all__ = [
    "HANDLER_NAME",
    "OtpDelivery",
    "deliver_challenge",
    "delivered_idempotency_key",
    "make_challenge_handler",
    "mint_code",
    "subscribe_challenges",
]

logger = logging.getLogger(__name__)

#: How the retry queue refers to this handler. Stable across deploys: renaming it
#: strands whatever retries are queued under the old name (see `webhooks/dispatch`).
HANDLER_NAME = "otp.deliver"


def delivered_idempotency_key(provider_id: str, challenge_id: str) -> str:
    """The ledger key for "we turned this challenge into a code".

    Keyed on the challenge rather than on the delivery, so a re-delivered webhook
    and a re-run handler collapse to the same row.
    """
    return f"otp:{provider_id}:{challenge_id}:delivered"


def mint_code(digits: int | None = None) -> str:
    """A code of our own, for a provider that expects us to issue the challenge.

    `secrets`, not `random`: a code an attacker can predict is not a second factor,
    and `random` is seeded well enough to be reproducible by anyone who cares to.
    Zero-padded, because a leading zero is part of a six-digit code and stripping it
    would produce a five-digit one the cardholder cannot enter.
    """
    width = digits if digits is not None else get_settings().otp_code_digits
    return f"{secrets.randbelow(10**width):0{width}d}"


@dataclass(frozen=True, slots=True)
class OtpDelivery:
    """What one challenge event became."""

    outcome: Remembered
    challenge: OtpChallenge


def _deadline(event: CardEvent, *, now: datetime) -> datetime:
    """When this challenge dies.

    The provider's own `expiry_time` wins when there is one — they know how long
    their ACS will wait, and a code that outlives the challenge is worse than no
    code, because the cardholder enters it and is declined. Absent one, the
    configured fallback. Capped either way: a payload claiming a week is a payload
    that would keep a secret in Redis for a week.
    """
    settings = get_settings()
    ceiling = now + timedelta(seconds=settings.otp_max_ttl_seconds)
    if event.challenge_expires_at is None:
        return min(now + timedelta(seconds=settings.otp_ttl_seconds), ceiling)
    return min(event.challenge_expires_at, ceiling)


def _challenge_from(event: CardEvent, *, now: datetime) -> OtpChallenge:
    code = event.otp_code
    return OtpChallenge(
        provider_id=event.provider_id,
        # SPEC.md §6.2 keys on "card + challenge id". A provider that names no
        # challenge still names the delivery, which is unique and is already the
        # basis of webhook dedup — a better fallback than one we invent.
        challenge_id=event.challenge_id or event.event_id,
        card_id=event.card_id,
        cardholder_id=event.cardholder_id,
        event_id=event.event_id,
        code=code or mint_code(),
        derived=code is None,
        delivered_at=event.occurred_at,
        expires_at=_deadline(event, now=now),
        amount_minor=event.amount.amount_minor if event.amount else None,
        currency=event.amount.currency if event.amount else None,
    )


async def deliver_challenge(
    session: AsyncSession, store: OtpStore, event: CardEvent, *, now: datetime
) -> OtpDelivery:
    """Store the code for one challenge and ledger that we did. Commits.

    Raises whatever the store or the ledger raises: a challenge we failed to store
    is one the cardholder cannot answer, so it belongs on the handler-failure path
    — retried with backoff, then dead-lettered — rather than being swallowed.
    """
    challenge = _challenge_from(event, now=now)
    outcome = await store.remember(challenge, now=now)

    if outcome is Remembered.EXPIRED:
        # Nothing can be done for the cardholder. Recorded rather than logged,
        # because "a challenge arrived and could not be served" is exactly the kind
        # of fact an operator has to be able to find after the fact.
        await _ledger_undeliverable(session, challenge, reason="expired", now=now)
        return OtpDelivery(outcome=outcome, challenge=challenge)

    # Attempted on ALREADY_KNOWN too — see the module docstring on the crash window.
    await _ledger_delivered(session, challenge)
    if outcome is Remembered.STORED:
        logger.info(
            "3DS challenge %s from %s is answerable until %s (code %s)",
            challenge.challenge_id,
            challenge.provider_id,
            challenge.expires_at.isoformat(),
            "minted by us" if challenge.derived else "sent by the provider",
        )
    return OtpDelivery(outcome=outcome, challenge=challenge)


async def _ledger_delivered(session: AsyncSession, challenge: OtpChallenge) -> None:
    key = delivered_idempotency_key(challenge.provider_id, challenge.challenge_id)
    if await find_by_idempotency_key(session, key) is not None:
        return
    await record(
        session,
        event_type=event_types.OTP_DELIVERED,
        occurred_at=challenge.delivered_at,
        provider_id=challenge.provider_id,
        cardholder_id=challenge.cardholder_id,
        card_id=challenge.card_id,
        amount=_amount_of(challenge),
        idempotency_key=key,
        # No code, ever. `raw` already went to the ledger with the provider's
        # delivery, redacted (§11.2); this row is about what we did with it.
        payload={
            "challenge_id": challenge.challenge_id,
            "provider_event_id": challenge.event_id,
            "derived": challenge.derived,
            "expires_at": challenge.expires_at.isoformat(),
        },
    )
    await session.commit()


async def _ledger_undeliverable(
    session: AsyncSession, challenge: OtpChallenge, *, reason: str, now: datetime
) -> None:
    await record(
        session,
        event_type=event_types.OTP_UNDELIVERABLE,
        occurred_at=challenge.delivered_at,
        provider_id=challenge.provider_id,
        cardholder_id=challenge.cardholder_id,
        card_id=challenge.card_id,
        amount=_amount_of(challenge),
        idempotency_key=f"otp:{challenge.provider_id}:{challenge.challenge_id}:{reason}",
        payload={
            "challenge_id": challenge.challenge_id,
            "provider_event_id": challenge.event_id,
            "reason": reason,
            "expires_at": challenge.expires_at.isoformat(),
            "observed_at": now.isoformat(),
        },
    )
    await session.commit()


def _amount_of(challenge: OtpChallenge) -> Money | None:
    if challenge.amount_minor is None or challenge.currency is None:
        return None
    return Money(challenge.amount_minor, challenge.currency)


def make_challenge_handler(sessionmaker: async_sessionmaker[AsyncSession], redis: Redis) -> Handler:
    """A handler with a session of its own, and the store it writes to.

    `Handler` takes only the event — that is what keeps `webhooks/` free of both
    the database and this package — so the consumer opens its own session per
    event, exactly as funding's settlement consumer does.
    """
    store = OtpStore(redis)

    async def handle(event: CardEvent) -> None:
        async with sessionmaker() as session:
            await deliver_challenge(session, store, event, now=utcnow())

    return handle


def subscribe_challenges(sessionmaker: async_sessionmaker[AsyncSession], redis: Redis) -> None:
    """Register the consumer. Called by the composition root, not at import.

    `replace=True` makes this idempotent: `create_app()` runs once per test in the
    suite, and re-registering the same consumer under the same name must not raise.
    """
    # idempotency key: (provider_id, challenge_id) — the store refuses a second
    # code under the same challenge, and the ledger row is keyed on the challenge
    # rather than the delivery, so a replayed webhook and a re-run handler both
    # collapse to one code and one row.
    subscribe(
        CardEventType.THREE_DS_CHALLENGE,
        HANDLER_NAME,
        make_challenge_handler(sessionmaker, redis),
        replace=True,
    )
