"""Stripe's webhook signature scheme, as published.

One header, `Stripe-Signature`, carrying a comma-delimited `key=value` list: `t`
is the Unix timestamp and each `v1` is a hex HMAC-SHA256 over
`"{t}.{raw body}"`. Several `v1` values can be present at once, because a rolled
endpoint secret stays live for up to 24 hours and Stripe signs once per secret.
A `v0` value appears on test events and is documented as *not* a valid
signature.

Three things diverge from `lithic/signing.py`, and each is a plausible wrong
answer rather than a detail:

**The key is the secret exactly as issued.** Lithic documents base64-decoding
the part after `whsec_`; Stripe documents "use the endpoint's signing secret as
the key" and never mentions decoding, so the `whsec_` prefix is part of the key.
The two rules produce different digests from the same-looking secret, and the
wrong one fails on every genuine delivery while passing every test that signs
with itself. `tests/test_stripe_signing.py` pins the divergence in both
directions.

**The digest is hex.** Lithic's is base64.

**The timestamp travels with the signature**, not in a header of its own — so
there is nothing to look up separately, and nothing to forget to cover.

What Stripe does *not* put in the envelope is a delivery id: the event id is in
the body (`evt_…`), which is inside the signed content anyway. See
`adapter.webhook_event_id`.

This module deliberately shares no code with the other adapters' signing
modules. A helper shared between two providers is how "adding an issuer is one
adapter package" becomes "one package, plus edits to whatever they share"
(docs/ARCHITECTURE.md §4.2).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime

#: Lower-case because that is what ASGI delivers; lookups are case-insensitive.
SIGNATURE_HEADER = "stripe-signature"

#: The field carrying the Unix timestamp inside the header.
TIMESTAMP_FIELD = "t"

#: The only scheme whose signatures are real.
SIGNATURE_SCHEME = "v1"

#: Present on test events and deliberately not verifiable. Named here so that
#: ignoring it is a decision with a test behind it rather than an oversight.
TEST_MODE_SCHEME = "v0"

#: Stripe's documented default tolerance, in either direction. Their own advice is
#: never to use 0, which disables the recency check altogether.
DEFAULT_TOLERANCE_SECONDS = 300

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_HEADER",
    "SIGNATURE_SCHEME",
    "TEST_MODE_SCHEME",
    "TIMESTAMP_FIELD",
    "header_value",
    "sign",
    "signature_header",
    "signed_content",
    "verify",
]


def signed_content(*, timestamp: str, body: bytes) -> bytes:
    """`"{t}.{raw body}"`, as bytes.

    The body stays bytes throughout: `json.loads` followed by `json.dumps`
    produces a different document for the same object and would break every
    verification.
    """
    return f"{timestamp}.".encode() + body


def sign(secret: str, *, timestamp: str, body: bytes) -> str:
    """One hex `v1` digest.

    Used by the tests and by fixture authoring. A real delivery is signed by
    Stripe; this is the code path that claims to compute what they do.
    """
    return hmac.new(
        secret.encode(), signed_content(timestamp=timestamp, body=body), hashlib.sha256
    ).hexdigest()


def signature_header(secret: str, *, timestamp: str, body: bytes) -> str:
    """A whole `Stripe-Signature` value, in the form Stripe sends it."""
    digest = sign(secret, timestamp=timestamp, body=body)
    return f"{TIMESTAMP_FIELD}={timestamp},{SIGNATURE_SCHEME}={digest}"


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup: ASGI lower-cases names, humans do not."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def parse_signature_header(value: str) -> tuple[str | None, tuple[str, ...]]:
    """The timestamp and every `v1` digest in one header value.

    Unknown fields are ignored rather than treated as malformed, so a field
    Stripe adds later does not start rejecting genuine deliveries. `v0` is
    ignored for the opposite reason: it is known, and known to be fake.
    """
    timestamp: str | None = None
    signatures: list[str] = []
    for part in value.split(","):
        field, _, encoded = part.strip().partition("=")
        if field == TIMESTAMP_FIELD and encoded:
            timestamp = encoded.strip()
        elif field == SIGNATURE_SCHEME and encoded:
            signatures.append(encoded.strip())
    return timestamp, tuple(signatures)


def verify(
    secret: str,
    *,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Authenticate a delivery. False for every kind of failure.

    Boolean by design: the caller answers 401 either way, and telling a caller
    which part of their forgery was closest is free help for them.

    An empty secret refuses everything. Unlike Lithic's, Stripe's key needs no
    decoding, so an empty secret is a *usable* HMAC key rather than an error —
    which would make an unconfigured endpoint verify anything an attacker signed
    with the empty string.
    """
    if not secret:
        return False

    offered = header_value(headers, SIGNATURE_HEADER)
    if not offered:
        return False

    timestamp, signatures = parse_signature_header(offered)
    if not timestamp or not signatures:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    # The timestamp is inside the signed content, so a captured delivery cannot be
    # relabelled with a fresh one.
    if abs(int(now.timestamp()) - sent_at) > tolerance_seconds:
        return False

    expected = hmac.new(
        secret.encode(), signed_content(timestamp=timestamp, body=body), hashlib.sha256
    ).digest()

    for candidate in signatures:
        try:
            decoded = bytes.fromhex(candidate)
        except ValueError:
            continue
        if hmac.compare_digest(expected, decoded):
            return True
    return False
