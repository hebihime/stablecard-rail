"""Gnosis Pay's webhook signature scheme, as far as the mock can honour it.

Shaped on https://docs.gnosispay.com/webhooks/getting-started: two headers —
`x-webhook-timestamp` (unix seconds) and `x-webhook-signature` (base64) — over the
signed material `"{timestamp}.{body}"`, with a receiving window so a captured
delivery cannot be replayed indefinitely.

**One deliberate divergence.** Gnosis Pay signs with *Ed25519* and publishes the
verifying key at `PUBLIC_KEY_URL`, which makes verification asymmetric: a partner
holds no secret at all. Ed25519 is not in the standard library, and a dependency
beyond SPEC.md §2 needs asking for, so the mock keeps the header names, the base64
encoding, the signed material and the window, and swaps the primitive for
HMAC-SHA256. What the pipeline exercises either way is a real MAC over the *raw*
body inside a timestamp window.

Note the raw `bytes` body throughout. Verifying against re-serialized JSON is the
classic way to break every provider's signature check.

Unlike Lithic's scheme there is **no event id here to sign**, because Gnosis Pay's
envelope has none — see `GnosisPayMockAdapter.webhook_event_id`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime

SIGNATURE_HEADER = "x-webhook-signature"
TIMESTAMP_HEADER = "x-webhook-timestamp"

#: Where a partner fetches the Ed25519 verifying key upstream. Recorded rather
#: than used: the mock is symmetric (see the module docstring).
PUBLIC_KEY_URL = "https://webhooks.gnosispay.com/api/v1/public-key"

#: How far a delivery's timestamp may be from now, in either direction.
DEFAULT_TOLERANCE_SECONDS = 300

#: Their scheme carries no key id and no version marker, unlike Lithic's `v1,…`:
#: on rotation a partner simply re-fetches the public key. Nothing to negotiate,
#: and nothing for the mock to model.
__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "PUBLIC_KEY_URL",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "header_value",
    "sign",
    "verify",
]


def _signed_material(*, timestamp: str, body: bytes) -> bytes:
    return b".".join((timestamp.encode(), body))


def _digest(secret: str, *, timestamp: str, body: bytes) -> str:
    material = _signed_material(timestamp=timestamp, body=body)
    mac = hmac.new(secret.encode(), material, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def sign(secret: str, *, timestamp: str, body: bytes) -> str:
    """Produce a `SIGNATURE_HEADER` value for a delivery."""
    return _digest(secret, timestamp=timestamp, body=body)


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
    if not signature or not timestamp:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(int(now.timestamp()) - sent_at) > tolerance_seconds:
        return False

    expected = _digest(secret, timestamp=timestamp, body=body)
    # Compare the base64 text, not the decoded bytes: an unparseable signature is
    # then just a mismatch rather than an exception on hostile input.
    return hmac.compare_digest(expected, signature)
