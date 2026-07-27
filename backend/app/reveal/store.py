"""Short-lived, single-use reveal tokens (SPEC.md §9.2).

Redis, because the deadline has to be enforced by something that keeps running when
we are not — the same argument `app/otp/store.py` makes about a code. The differences
from that store are all consequences of one fact: an OTP code is *meant* to be read
by a human, and a reveal token is a bearer credential.

* It is stored under **a hash of itself**. A code has to come back out of the store
  intact; a token only has to be recognised, and `sha256` recognises it without the
  store ever holding anything usable.
* Redemption is **`GETDEL`** — one atomic operation. Read-then-delete would let two
  simultaneous requests both find the token present, which loses the only property
  the token has.
* A spent token leaves **a marker**, so a replay is legible as a replay. That
  distinction is for the ledger and the logs, never for the client: `api/reveal.py`
  answers 404 to both, because telling an attacker which of their guesses was once
  real is a free hint.

`now` is a parameter here as everywhere else in this codebase — code that reads its
own clock can only be tested by waiting.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from app.core.time import UtcDatetime

__all__ = [
    "KEY_PREFIX",
    "SPENT_PREFIX",
    "MintedRevealToken",
    "RevealGrant",
    "RevealRejection",
    "RevealRejectionReason",
    "RevealTokenStore",
    "spent_key",
    "token_key",
]

logger = logging.getLogger(__name__)

#: Namespace for live tokens, keyed by digest.
KEY_PREFIX = "reveal:token"

#: Namespace for the "this one has been spent" markers.
SPENT_PREFIX = "reveal:spent"

#: Bytes of entropy per token. 32 gives a 43-character `urlsafe` string; a token
#: that can be guessed inside its own 60-second life would make the rest of this
#: file decorative.
TOKEN_BYTES = 32


def token_key(digest: str) -> str:
    return f"{KEY_PREFIX}:{digest}"


def spent_key(digest: str) -> str:
    return f"{SPENT_PREFIX}:{digest}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RevealRejectionReason(StrEnum):
    """Why a redemption did not produce a grant.

    Two values rather than `None`, because the caller ledgers them differently: a
    replay names a token we really did mint, and is worth recording against the card
    it was minted for; an unknown token is somebody trying, and belongs in the log
    with nothing to attribute it to.
    """

    UNKNOWN = "unknown"
    REPLAYED = "replayed"


class RevealRejection(BaseModel):
    """A refusal, and whatever attribution honestly exists for it.

    A replay can name its card because the spent marker remembers what the token was
    minted for. An unrecognised token names nothing, and the fields stay `None`
    rather than being filled with a guess — a ledger row claiming an attack on a
    specific card, when no such card was named, is worse than one that admits it
    does not know.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: RevealRejectionReason
    provider_id: str | None = None
    card_id: str | None = None


class RevealGrant(BaseModel):
    """What a redeemed token entitles the bearer to, and nothing more.

    Deliberately carries no token: it is what remains *after* the credential has been
    spent, and a grant that could be re-sent would undo the exchange.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    card_id: str
    minted_at: UtcDatetime
    expires_at: UtcDatetime


class MintedRevealToken(BaseModel):
    """A freshly minted token, on its way out to exactly one client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The only time this value exists outside the client's hands.
    token: str = Field(repr=False)
    provider_id: str
    card_id: str
    expires_at: UtcDatetime
    #: Sent alongside the deadline so the countdown does not depend on the client's
    #: clock — the same reasoning as `PendingChallengeOut.seconds_remaining`.
    expires_in: int


class RevealTokenStore:
    """Mint and redeem, once each."""

    def __init__(self, redis: Redis, *, ttl_seconds: int, replay_memory_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"reveal token TTL must be positive, got {ttl_seconds}")
        if replay_memory_seconds <= 0:
            raise ValueError(f"replay memory must be positive, got {replay_memory_seconds}")
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._replay_memory_seconds = replay_memory_seconds

    async def mint(self, provider_id: str, card_id: str, *, now: datetime) -> MintedRevealToken:
        """Issue a token for one card.

        No `nx` and no reuse: unlike an OTP code, two tokens for the same card are
        not a conflict. A cardholder who closes the reveal screen and opens it again
        is doing something entirely ordinary, and the first token stays valid for
        whatever is left of its own minute.
        """
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        grant = RevealGrant(
            provider_id=provider_id, card_id=card_id, minted_at=now, expires_at=expires_at
        )
        await self._redis.set(
            token_key(_digest(token)), grant.model_dump_json(), ex=self._ttl_seconds
        )
        return MintedRevealToken(
            token=token,
            provider_id=provider_id,
            card_id=card_id,
            expires_at=expires_at,
            expires_in=self._ttl_seconds,
        )

    async def redeem(self, token: str) -> RevealGrant | RevealRejection:
        """Spend a token, atomically, and say what happened.

        `GETDEL` is the whole single-use guarantee. Two requests carrying the same
        token reach Redis in some order; exactly one gets the value and the other
        gets `None`, whatever the interleaving upstream.

        **No `now` here**, unlike every other timed operation in this codebase. The
        deadline is Redis's to enforce and this method compares no clocks — taking a
        `now` it did not use would imply a check that is not happening.
        """
        digest = _digest(token)
        raw = await self._redis.getdel(token_key(digest))
        if raw is None:
            spent = await self._redis.get(spent_key(digest))
            if spent is not None:
                logger.warning("reveal token replayed (digest %s...)", digest[:12])
                previous = RevealGrant.model_validate_json(spent)
                return RevealRejection(
                    reason=RevealRejectionReason.REPLAYED,
                    provider_id=previous.provider_id,
                    card_id=previous.card_id,
                )
            # Never minted, expired, or minted longer ago than we remember. All
            # indistinguishable from here, and all "no".
            logger.info("reveal token not recognised (digest %s...)", digest[:12])
            return RevealRejection(reason=RevealRejectionReason.UNKNOWN)

        # After the delete, so a crash between the two loses the marker rather than
        # the token: a forgotten marker downgrades a future replay to "unknown",
        # while a marker with the token still live would refuse a legitimate first use.
        # The marker holds the grant, not a timestamp, so a replay stays attributable.
        await self._redis.set(spent_key(digest), raw, ex=self._replay_memory_seconds)
        return RevealGrant.model_validate_json(raw)
