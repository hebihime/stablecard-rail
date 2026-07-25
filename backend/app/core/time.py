"""Timestamps, and the one rule about them: they are UTC and they are aware.

A naive datetime in a funding pipeline is a bug waiting for a clock change. It is
also unanswerable — nothing in the value records which zone it was written in, so
a comparison against a provider's `occurred_at` is a guess. Rejecting them at
construction is the only place the question has an answer.

Shared vocabulary rather than one module's helper, for the reason
docs/ARCHITECTURE.md §2.8 gives about `Money`: `issuers/`, `ledger/`, `funding/`
and `chain/` all need it, and no module outside `issuers/` may import from it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

__all__ = ["UtcDatetime", "require_utc", "utcnow"]


def require_utc(value: datetime) -> datetime:
    """The same value in UTC, or a `ValueError` if it never said which zone."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC; a naive value is ambiguous")
    return value.astimezone(UTC)


#: Every timestamp crossing a module boundary is stored and compared in UTC.
UtcDatetime = Annotated[datetime, AfterValidator(require_utc)]


def utcnow() -> datetime:
    """Now, aware and in UTC.

    A named function rather than `datetime.now(UTC)` at each call site because it
    is the seam every clock-dependent component takes as an argument — the bridge
    simulator's latency, the reconciler's staleness threshold — so tests can hand
    them a clock they control instead of sleeping.
    """
    return datetime.now(UTC)
