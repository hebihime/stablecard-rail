"""Where a one-time code lives, and for how long (SPEC.md §6.2).

Redis, keyed to `(provider_id, challenge_id)`, with the TTL taken from the
challenge's own deadline. Two structures, because the two questions are different:

* **the code itself** — one key per challenge, carrying its own expiry. Redis
  forgets it whether or not anything of ours is still running, which is the only
  way a secret with a deadline can be trusted to have one.
* **an index of what is open** — a sorted set scored by expiry, so `GET
  /otp/pending` is one range query instead of a `SCAN` over a live keyspace.

The two expire independently: Redis has no per-member TTL for a sorted set, so an
index entry can outlive the code it points at. That is not a bug to design out —
it is the normal state after an eviction — so reads treat the *code* as
authoritative and prune the index as they go.

Every operation takes `now` rather than reading the clock, for the reason
`webhooks/retry.py` gives: code that reads its own clock can only be tested by
waiting.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

from app.core.time import UtcDatetime

__all__ = [
    "DEFAULT_INDEX_KEY",
    "KEY_PREFIX",
    "OtpChallenge",
    "OtpStore",
    "Remembered",
    "challenge_key",
]

logger = logging.getLogger(__name__)

#: Namespace for the codes themselves. Separate from `webhook:` and `otp:pending`
#: so that a `FLUSHDB` is never the only way to reason about what is stored where.
KEY_PREFIX = "otp:challenge"

#: The sorted set of open challenges, scored by when each one dies.
DEFAULT_INDEX_KEY = "otp:pending"


def challenge_key(provider_id: str, challenge_id: str) -> str:
    """The Redis key for one challenge.

    Namespaced by provider for the same reason `webhooks/dedup.py` namespaces its
    claims: two providers numbering their challenges from 1 is normal, and a
    collision here would show one provider's code for another's challenge.
    """
    return f"{KEY_PREFIX}:{provider_id}:{challenge_id}"


def _member(provider_id: str, challenge_id: str) -> str:
    return f"{provider_id}:{challenge_id}"


class Remembered(StrEnum):
    """What happened to a `remember()` call.

    Three answers rather than a boolean, because the caller does something
    different with each: `STORED` is worth a ledger row and a push, `ALREADY_KNOWN`
    is a retry doing no harm, and `EXPIRED` is a challenge that arrived dead and
    needs saying out loud rather than silently succeeding.
    """

    STORED = "stored"
    ALREADY_KNOWN = "already_known"
    EXPIRED = "expired"


class OtpChallenge(BaseModel):
    """An open 3DS challenge and the code that answers it.

    Frozen, and JSON is how it reaches Redis. `code` is a plain field here — unlike
    `CardEvent.otp_code`, which is excluded from every serializer — because this
    model exists precisely to be written to the one store that forgets it
    (docs/ARCHITECTURE.md §11.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    challenge_id: str
    #: The provider's card id, when the challenge names one. `None` is possible: a
    #: challenge is about an authentication, and not every provider ties it to a
    #: card in the payload.
    card_id: str | None = None
    cardholder_id: str | None = None
    #: The delivery that carried the challenge — the link back to the ledger row.
    event_id: str
    code: str
    #: `True` when we minted the code rather than reading it from the provider.
    #: Recorded because it changes what the code *is*: ours to verify, or theirs.
    derived: bool = False
    delivered_at: UtcDatetime
    expires_at: UtcDatetime
    amount_minor: int | None = None
    currency: str | None = None

    def seconds_left(self, now: datetime) -> int:
        """Whole seconds until this challenge dies; 0 once it has.

        Rounded **up**: `EX` takes integer seconds, and truncating 0.4s to zero
        would either be rejected by Redis or drop a challenge that is still alive.
        """
        return max(0, math.ceil((self.expires_at - now).total_seconds()))


class OtpStore:
    """Create-only, self-expiring storage for open challenges."""

    def __init__(self, redis: Redis, *, index_key: str = DEFAULT_INDEX_KEY) -> None:
        self._redis = redis
        self._index_key = index_key

    @property
    def index_key(self) -> str:
        return self._index_key

    async def remember(self, challenge: OtpChallenge, *, now: datetime) -> Remembered:
        """Store a challenge, unless one is already stored under its id.

        **`nx=True` is the whole idempotency story.** This runs from a webhook
        handler, and a handler can run more than once: dispatch fails and the retry
        queue re-runs it, or a second worker drains the same item. If the code were
        overwritten, the value the app is already showing — which the cardholder has
        read and may be typing — would silently stop being the right one. So the
        first write wins and the caller is told that it did not.
        """
        ttl = challenge.seconds_left(now)
        if ttl == 0:
            logger.warning(
                "3DS challenge %s from %s arrived already expired (deadline %s, now %s)",
                challenge.challenge_id,
                challenge.provider_id,
                challenge.expires_at.isoformat(),
                now.isoformat(),
            )
            return Remembered.EXPIRED

        stored = await self._redis.set(
            challenge_key(challenge.provider_id, challenge.challenge_id),
            challenge.model_dump_json(),
            nx=True,
            ex=ttl,
        )
        if not stored:
            return Remembered.ALREADY_KNOWN

        # The index second: an entry pointing at a code that exists is recoverable
        # (a read prunes it), whereas a code with no entry would be invisible to
        # every poll.
        member = _member(challenge.provider_id, challenge.challenge_id)
        await self._redis.zadd(self._index_key, {member: challenge.expires_at.timestamp()})
        return Remembered.STORED

    async def get(self, provider_id: str, challenge_id: str) -> OtpChallenge | None:
        raw = await self._redis.get(challenge_key(provider_id, challenge_id))
        if raw is None:
            return None
        return OtpChallenge.model_validate_json(raw)

    async def pending(
        self, *, now: datetime, card_id: str | None = None, limit: int = 50
    ) -> list[OtpChallenge]:
        """Open challenges, soonest deadline first.

        The card filter is applied after the range query rather than by keeping an
        index per card: at this scale the open set is bounded by the TTL and is
        tiny, and a second index would need a fan-out delete on every answer — one
        more thing to leave stale.
        """
        # The client this store is given always decodes responses
        # (app/core/redis.py), which redis-py's own annotations cannot express.
        members = cast(
            "list[str]",
            await self._redis.zrangebyscore(
                self._index_key, min=now.timestamp(), max="+inf", start=0, num=limit
            ),
        )
        found: list[OtpChallenge] = []
        stale: list[str] = []
        for member in members:
            provider_id, _, challenge_id = member.partition(":")
            challenge = await self.get(provider_id, challenge_id)
            if challenge is None:
                stale.append(member)
                continue
            if card_id is None or challenge.card_id == card_id:
                found.append(challenge)
        if stale:
            # The code expired or was evicted and took no index entry with it.
            # Reading is the only operation that finds out, so it is the one that
            # tidies up — otherwise every poll rediscovers the same ghosts.
            await self._redis.zrem(self._index_key, *stale)
            logger.debug("pruned %s stale OTP index entries", len(stale))
        return found

    async def forget(self, provider_id: str, challenge_id: str) -> bool:
        """Consume a challenge. `True` if there was one to consume.

        A challenge is single-use (SPEC.md §6.5): once it has been answered it is
        neither pending nor answerable again, and the caller needs to tell "answered"
        from "answered twice". The index entry goes either way — a code that has
        expired on its own still leaves one behind.
        """
        removed = await self._redis.delete(challenge_key(provider_id, challenge_id))
        await self._redis.zrem(self._index_key, _member(provider_id, challenge_id))
        return bool(removed)
