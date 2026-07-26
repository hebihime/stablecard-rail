"""The push channel and `/ws/otp` (SPEC.md §6.3).

Two layers, tested separately because they fail differently:

* `app/otp/push.py` — Redis pub/sub, against real Redis. What is asserted here is
  the *absence* of retention: a message published with nobody listening is gone,
  which is the property the whole design rests on.
* the WebSocket endpoint — driven with a fake socket rather than a real handshake.
  A real one needs a second event loop (starlette's `TestClient` runs the app in a
  portal of its own) while `redis.asyncio` connections belong to the loop that
  opened them, so the two cannot share a client. The endpoint is a plain async
  function, so calling it directly tests the same code, and the socket's own
  behaviour — accept, send, disconnect — is starlette's to get right, not ours.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from fastapi import WebSocketDisconnect
from redis.asyncio import Redis

from app.api.otp import push_challenges
from app.core.time import utcnow
from app.main import create_app
from app.otp.push import (
    EVERY_CHANNEL,
    channel_for,
    consume_acknowledgement,
    listen,
    publish_challenge,
    subscription,
)
from app.otp.store import OtpChallenge, OtpStore
from tests.support import routed_paths

CODE = "918273"


def challenge(
    *, challenge_id: str = "3ds_000001", card_id: str | None = "card_1", code: str = CODE
) -> OtpChallenge:
    now = utcnow()
    return OtpChallenge(
        provider_id="gnosis_pay_mock",
        challenge_id=challenge_id,
        card_id=card_id,
        cardholder_id="user_1",
        event_id="evt-1",
        code=code,
        delivered_at=now,
        expires_at=now + timedelta(minutes=5),
    )


# ----------------------------------------------------------------- channels ----


def test_a_challenge_with_no_card_gets_a_channel_of_its_own() -> None:
    # "No card" is not a card id. Sharing a channel with one would deliver a
    # challenge to a subscriber who asked for something else.
    assert "otp:push:card_1" == channel_for("card_1")
    assert "otp:push:-" == channel_for(None)
    assert channel_for(None) != channel_for("")


def test_the_every_card_pattern_covers_both() -> None:
    prefix = EVERY_CHANNEL.removesuffix("*")
    assert channel_for("card_1").startswith(prefix)
    assert channel_for(None).startswith(prefix)


# ------------------------------------------------------------- no retention ----


async def test_a_push_with_nobody_listening_is_simply_gone(redis_client: Redis) -> None:
    """The property the design depends on, asserted rather than assumed.

    If pub/sub retained anything, a code would outlive its challenge in a store
    nobody expires — which is the whole reason this is not the `EventBus` stream.
    """
    assert 0 == await publish_challenge(redis_client, challenge())

    async with subscription(redis_client, card_id="card_1") as pubsub:
        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) is None


async def test_a_listener_receives_a_challenge_whole(redis_client: Redis) -> None:
    received: list[OtpChallenge] = []

    async def reader() -> None:
        async for pushed in listen(redis_client, card_id="card_1", poll_seconds=0.05):
            received.append(pushed)
            return

    task = asyncio.create_task(reader())
    await _publish_until_delivered(redis_client, challenge())
    await asyncio.wait_for(task, timeout=2)

    assert 1 == len(received)
    assert "3ds_000001" == received[0].challenge_id
    assert CODE == received[0].code


async def test_a_listener_on_one_card_is_not_woken_by_another(redis_client: Redis) -> None:
    async with subscription(redis_client, card_id="card_1") as pubsub:
        await publish_challenge(redis_client, challenge(card_id="card_2"))

        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) is None


async def test_a_listener_that_named_no_card_hears_every_card(redis_client: Redis) -> None:
    async with subscription(redis_client) as pubsub:
        await publish_challenge(redis_client, challenge(card_id="card_9"))

        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        assert message is not None
        assert CODE in message["data"]


class ScriptedPubSub:
    """A `PubSub` that answers a fixed sequence, to reach what Redis will not.

    Two cases live here because real Redis cannot be made to produce them: an
    acknowledgement that never arrives, and a message arriving ahead of one. The
    second is the interesting one — Redis does not deliver on a subscription it has
    not confirmed, so if it ever happened it would mean the assumption behind
    `consume_acknowledgement` reading exactly once had broken.
    """

    def __init__(self, *messages: dict[str, Any] | None) -> None:
        self._messages = list(messages)

    async def get_message(self, **_: Any) -> dict[str, Any] | None:
        return self._messages.pop(0) if self._messages else None


async def test_a_missing_acknowledgement_is_reported_rather_than_waited_on() -> None:
    # Reported, not raised: the only cost is the swallowed first read the helper
    # exists to prevent, and blocking a WebSocket handshake over it would be worse.
    assert await consume_acknowledgement(ScriptedPubSub(None), wait_seconds=0.01) is False  # type: ignore[arg-type]


async def test_a_message_ahead_of_the_acknowledgement_is_not_discarded() -> None:
    # One read, not a drain. Draining until an acknowledgement appeared would throw
    # this challenge away; saying "no acknowledgement seen" leaves it on the wire.
    scripted = ScriptedPubSub({"type": "message", "data": "{}"}, {"type": "subscribe"})

    assert await consume_acknowledgement(scripted, wait_seconds=0.01) is False  # type: ignore[arg-type]


async def test_subscribing_swallows_its_own_acknowledgement(redis_client: Redis) -> None:
    """Otherwise every "nothing was pushed" assertion in this file is vacuous.

    redis-py delivers the `SUBSCRIBE` acknowledgement as an ordinary message, so a
    caller's first `get_message(ignore_subscribe_messages=True)` consumes it and
    answers `None` — whether or not a real message was queued behind it. So
    `subscription()` reads it before yielding, and this asserts that the very first
    read a caller makes is the challenge rather than the acknowledgement.
    """
    async with subscription(redis_client, card_id="card_1") as pubsub:
        await publish_challenge(redis_client, challenge())

        first = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert first is not None
    assert "message" == first["type"]


async def test_an_unreadable_payload_does_not_end_the_subscription(
    redis_client: Redis,
) -> None:
    # Something else on our namespace, or a version skew. One bad message must not
    # disconnect a cardholder mid-challenge.
    received: list[OtpChallenge] = []

    async def reader() -> None:
        async for pushed in listen(redis_client, card_id="card_1", poll_seconds=0.05):
            received.append(pushed)
            return

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.1)
    await redis_client.publish(channel_for("card_1"), "not a challenge")
    await _publish_until_delivered(redis_client, challenge())
    await asyncio.wait_for(task, timeout=2)

    assert 1 == len(received)


async def test_a_subscription_releases_its_connection(redis_client: Redis) -> None:
    # A WebSocket per client and a leaked connection per socket is the failure mode
    # a repeatedly-reloaded demo finds first.
    async with subscription(redis_client, card_id="card_1") as pubsub:
        assert pubsub.subscribed

    assert not pubsub.subscribed


# ------------------------------------------------------------- the endpoint ----


class FakeWebSocket:
    """Enough of a WebSocket to drive the endpoint, and to end it on cue.

    `send_json` raising `WebSocketDisconnect` after `disconnect_after` messages is
    how a real client going away presents itself, and it is what stops the
    endpoint's infinite loop.
    """

    def __init__(self, *, disconnect_after: int = 1) -> None:
        self.accepted = False
        self.sent: list[dict[str, Any]] = []
        self._disconnect_after = disconnect_after

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        if len(self.sent) >= self._disconnect_after:
            raise WebSocketDisconnect(code=1000)


async def test_the_socket_sends_what_is_already_open_on_connect(redis_client: Redis) -> None:
    """A client that connects a second after the webhook must not be blind.

    Without this, the challenge would only appear on the *next* one — and there is
    no next one — so the cardholder would sit in front of a payment they cannot
    confirm despite the code being in the store all along.
    """
    now = utcnow()
    await OtpStore(redis_client).remember(challenge(), now=now)
    socket = FakeWebSocket(disconnect_after=1)

    await push_challenges(socket, redis_client)  # type: ignore[arg-type]

    assert socket.accepted is True
    assert 1 == len(socket.sent)
    assert "3ds_000001" == socket.sent[0]["challenge_id"]
    assert CODE == socket.sent[0]["code"]


async def test_the_socket_sends_the_same_shape_the_poll_endpoint_returns(
    redis_client: Redis, client: Any
) -> None:
    # So a client handles both paths with one code path and dedups on challenge_id.
    await OtpStore(redis_client).remember(challenge(), now=utcnow())
    socket = FakeWebSocket(disconnect_after=1)

    await push_challenges(socket, redis_client)  # type: ignore[arg-type]
    polled = (await client.get("/otp/pending")).json()["challenges"][0]

    assert set(polled) == set(socket.sent[0])


async def test_the_socket_streams_a_challenge_that_arrives_while_connected(
    redis_client: Redis,
) -> None:
    socket = FakeWebSocket(disconnect_after=1)

    task = asyncio.create_task(push_challenges(socket, redis_client))  # type: ignore[arg-type]
    await _publish_until_delivered(redis_client, challenge())
    await asyncio.wait_for(task, timeout=2)

    assert 1 == len(socket.sent)
    assert "3ds_000001" == socket.sent[0]["challenge_id"]


async def test_the_socket_narrows_to_one_card(redis_client: Redis) -> None:
    now = utcnow()
    store = OtpStore(redis_client)
    await store.remember(challenge(challenge_id="a", card_id="card_1"), now=now)
    await store.remember(challenge(challenge_id="b", card_id="card_2"), now=now)
    socket = FakeWebSocket(disconnect_after=1)

    await push_challenges(socket, redis_client, card_id="card_2")  # type: ignore[arg-type]

    assert ["b"] == [message["challenge_id"] for message in socket.sent]


async def test_a_disconnect_is_not_an_error(redis_client: Redis) -> None:
    # It is the normal end of every socket: an app backgrounded, a reload, a blip.
    # The challenge is still in the store for the next poll or the next connect.
    await OtpStore(redis_client).remember(challenge(), now=utcnow())
    socket = FakeWebSocket(disconnect_after=1)

    await push_challenges(socket, redis_client)  # type: ignore[arg-type]

    assert await OtpStore(redis_client).get("gnosis_pay_mock", "3ds_000001") is not None


def test_the_running_app_serves_the_socket() -> None:
    # The route half of §9.15's trap: a handler nobody routes to is a handler
    # nobody can reach. `routed_paths` walks recursively for a reason — see its
    # docstring, and the four-docs-endpoints answer the naive version gives.
    paths = routed_paths(create_app())

    assert "/ws/otp" in paths
    assert "/otp/pending" in paths


# --------------------------------------------------------------- plumbing ----


async def _publish_until_delivered(redis: Redis, item: OtpChallenge) -> None:
    """Publish until somebody is listening.

    A subscriber started as a task has not necessarily reached `SUBSCRIBE` by the
    time the publisher runs, and pub/sub keeps nothing for a late arrival — so a
    single publish is a race. Retrying until Redis reports a receiver is the
    deterministic version, and it is a fair test of the real behaviour: a push to
    nobody really is lost.
    """
    for _ in range(100):
        if await publish_challenge(redis, item):
            return
        await asyncio.sleep(0.02)
    pytest.fail("no subscriber ever appeared")
