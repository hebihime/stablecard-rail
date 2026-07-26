"""The push channel (SPEC.md §6.3): Redis pub/sub, deliberately not the EventBus.

The service already has a message bus — Redis Streams, behind the `EventBus`
interface — and this does not use it. The reason is the one thing that makes OTP
different from everything else here: **the stream is a replayable log, and a code
must stop existing.**

A stream entry is durable by design (that is what makes a consumer resumable, and
what makes a Kafka implementation a drop-in). Publishing a code onto it would put
the one value in the system that has a deadline into the one store built to ignore
deadlines. Pub/sub has no retention at all: a message reaches whoever is listening
at that instant and is then gone. Here that is not a weakness to work around, it is
the requirement.

What follows from having no retention is the division of labour SPEC.md §6.3 asks
for: **push is a courtesy, polling is the contract.** A client that is not
connected misses the message and finds the challenge on its next
`GET /otp/pending`. So nothing here retries, acknowledges, or persists — those
would all be attempts to make a fire-and-forget channel reliable, which is what the
poll endpoint is already for.

One channel per card, so a subscriber interested in one card is not woken by
another's. A challenge that names no card goes to a channel of its own rather than
to a shared one, because "no card" is not a card id and must not collide with one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.otp.store import OtpChallenge

__all__ = [
    "ACKNOWLEDGEMENTS",
    "CHANNEL_PREFIX",
    "EVERY_CHANNEL",
    "NO_CARD",
    "channel_for",
    "consume_acknowledgement",
    "listen",
    "publish_challenge",
    "subscription",
]

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "otp:push"

#: Stands in for a challenge that names no card. A literal that cannot be a real
#: provider card id, so a subscriber asking for one card never receives these.
NO_CARD = "-"

#: Pattern for "every card", used by a subscriber that did not name one.
EVERY_CHANNEL = f"{CHANNEL_PREFIX}:*"


def channel_for(card_id: str | None) -> str:
    """The channel one card's challenges are pushed to.

    `is None` rather than falsiness: adapters normalize an empty provider id to
    `None` already, but a function that maps `""` and `None` to the same channel is
    one that would deliver a challenge to a subscriber who asked for something else
    if that ever stopped being true.
    """
    return f"{CHANNEL_PREFIX}:{NO_CARD if card_id is None else card_id}"


async def publish_challenge(redis: Redis, challenge: OtpChallenge) -> int:
    """Push one challenge to whoever is listening. Returns how many that was.

    The count is returned because it is the only feedback this channel offers, and
    it is worth logging: nobody listening is entirely normal — the app is closed,
    or between reconnects — and is not a failure.
    """
    receivers = int(
        await redis.publish(channel_for(challenge.card_id), challenge.model_dump_json())
    )
    logger.debug("pushed 3DS challenge %s to %s listener(s)", challenge.challenge_id, receivers)
    return receivers


#: What Redis calls its two subscribe acknowledgements.
ACKNOWLEDGEMENTS = ("subscribe", "psubscribe")


async def consume_acknowledgement(pubsub: PubSub, *, wait_seconds: float = 1.0) -> bool:
    """Read the `SUBSCRIBE` acknowledgement off the connection before yielding.

    redis-py delivers it as a message like any other, so the *first*
    `get_message(ignore_subscribe_messages=True)` a caller makes consumes the
    acknowledgement and answers `None` — whether or not a real message was waiting
    behind it. Worth a round trip here rather than leaving it to every caller,
    because the failure it causes is silent in exactly one direction: "assert
    nothing was pushed" passes even when something was.

    **One read, not a loop.** Redis cannot deliver a message on a subscription
    before confirming it, so the acknowledgement is always first on the wire — and
    draining until one appears would throw away a challenge if that were ever
    untrue. Returns whether one was seen, so a caller that cares can say so; the
    only cost of missing it is the swallowed first read this exists to prevent.
    """
    message = await pubsub.get_message(timeout=wait_seconds)
    if message is not None and message["type"] in ACKNOWLEDGEMENTS:
        return True
    logger.debug(
        "no subscribe acknowledgement within %ss; the first read may consume it", wait_seconds
    )
    return False


@asynccontextmanager
async def subscription(redis: Redis, *, card_id: str | None = None) -> AsyncIterator[PubSub]:
    """Subscribe to one card's channel, or to every card's.

    A context manager because a subscription holds a connection: a WebSocket that
    goes away without releasing it leaks one per client, which is the kind of thing
    that only shows up under a demo being reloaded repeatedly.
    """
    pubsub = redis.pubsub()
    try:
        if card_id is None:
            await pubsub.psubscribe(EVERY_CHANNEL)
        else:
            await pubsub.subscribe(channel_for(card_id))
        await consume_acknowledgement(pubsub)
        yield pubsub
    finally:
        # redis-py ships `PubSub.aclose` without annotations, so `mypy --strict`
        # calls it untyped. Releasing the connection matters more than the ignore.
        await pubsub.aclose()  # type: ignore[no-untyped-call]


async def listen(
    redis: Redis, *, card_id: str | None = None, poll_seconds: float = 1.0
) -> AsyncIterator[OtpChallenge]:
    """Yield challenges as they are pushed, until the caller stops asking.

    `get_message` with a timeout rather than `listen()`: the timeout is what lets
    the surrounding task notice cancellation — a `listen()` loop blocked on a socket
    with no traffic cannot be interrupted at a useful point, and a WebSocket client
    disconnecting is exactly that situation.
    """
    async with subscription(redis, card_id=card_id) as pubsub:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_seconds)
            if message is None:
                continue
            try:
                yield OtpChallenge.model_validate_json(message["data"])
            except ValidationError:
                # Someone else's message on our namespace. Dropped rather than
                # raised: one bad payload must not end a live subscriber's session.
                logger.warning("ignoring an unreadable OTP push payload")
