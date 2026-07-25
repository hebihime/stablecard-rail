"""The mock provider's webhook signature scheme.

Modelled on how real issuers do it (Lithic's HMAC, Stripe's `Stripe-Signature`):
a versioned HMAC-SHA256 over a payload that includes a timestamp, so a captured
delivery cannot be replayed indefinitely.

The signed material is `timestamp.event_id.body`. Covering the **event id**
matters: it is the dedup key (SPEC.md §4), so if the signature did not include it
an attacker could re-send a legitimate body under a fresh id and have it
processed a second time.

Note the raw `bytes` body throughout. Verifying against re-serialized JSON is the
classic way to break every provider's signature check.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime

SIGNATURE_HEADER = "x-mock-signature"
TIMESTAMP_HEADER = "x-mock-timestamp"
EVENT_ID_HEADER = "x-mock-event-id"

#: Version prefix, so the scheme can change without a flag day.
SIGNATURE_VERSION = "v1"

#: How far a delivery's timestamp may be from now, in either direction.
DEFAULT_TOLERANCE_SECONDS = 300

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "EVENT_ID_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "header_value",
    "sign",
    "verify",
]


def _signed_material(*, timestamp: str, event_id: str, body: bytes) -> bytes:
    return b".".join((timestamp.encode(), event_id.encode(), body))


def _digest(secret: str, *, timestamp: str, event_id: str, body: bytes) -> str:
    material = _signed_material(timestamp=timestamp, event_id=event_id, body=body)
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def sign(secret: str, *, timestamp: str, event_id: str, body: bytes) -> str:
    """Produce a `SIGNATURE_HEADER` value for a delivery."""
    digest = _digest(secret, timestamp=timestamp, event_id=event_id, body=body)
    return f"{SIGNATURE_VERSION}={digest}"


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    ASGI lower-cases header names; a `curl` user or a test dict will not.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def verify(
    secret: str,
    *,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Authenticate a delivery. Returns False for every kind of failure.

    Deliberately boolean: the caller answers 401 either way, and distinguishing
    "wrong signature" from "malformed header" in a response tells an attacker
    which of their guesses was closer.
    """
    signature = header_value(headers, SIGNATURE_HEADER)
    timestamp = header_value(headers, TIMESTAMP_HEADER)
    event_id = header_value(headers, EVENT_ID_HEADER)
    if not signature or not timestamp or not event_id:
        return False

    version, _, digest = signature.partition("=")
    if version != SIGNATURE_VERSION or not digest:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(int(now.timestamp()) - sent_at) > tolerance_seconds:
        return False

    expected = _digest(secret, timestamp=timestamp, event_id=event_id, body=body)
    return hmac.compare_digest(expected, digest)
