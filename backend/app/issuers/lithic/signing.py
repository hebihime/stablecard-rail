"""Lithic's webhook signature scheme, as published.

Three headers, and a signed content of `"{webhook-id}.{webhook-timestamp}.{body}"`
HMAC-SHA256'd with a key that is the **base64-decoded** portion of the secret after
its `whsec_` prefix. The signature header carries `v1,<base64 digest>` values,
space-delimited so that several can be live at once during a secret rotation.

Two things worth pointing out, because they are load-bearing rather than incidental:

**The `webhook-id` is inside the signed content.** It is also the dedup key
(SPEC.md §4), so covering it is what stops a captured delivery from being re-sent
under a fresh id and processed twice. Phase 2's decision to dedup on an envelope id
before parsing lands exactly right here.

**The body is bytes and stays bytes.** `json.loads` then `json.dumps` produces
different bytes for the same document, and would break every verification.

This module deliberately shares no code with `evm_deposit_mock/signing.py`. Adapters
that reach into each other are how "adding an issuer is one adapter file" quietly
becomes "one file, plus edits to whatever they share"
(docs/ARCHITECTURE.md §4.2).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime

WEBHOOK_ID_HEADER = "webhook-id"
TIMESTAMP_HEADER = "webhook-timestamp"
SIGNATURE_HEADER = "webhook-signature"

#: The only signature version Lithic currently sends. Anything else is ignored
#: rather than guessed at.
SIGNATURE_VERSION = "v1"

#: Lithic hands out secrets as `whsec_<base64>`; the prefix is not part of the key.
SECRET_PREFIX = "whsec_"

#: Lithic's documented recommendation for the replay window, in either direction.
DEFAULT_TOLERANCE_SECONDS = 300

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SECRET_PREFIX",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "WEBHOOK_ID_HEADER",
    "header_value",
    "sign",
    "signing_key",
    "verify",
]


def signing_key(secret: str) -> bytes:
    """The HMAC key for a webhook secret.

    Raises `ValueError` for a secret that cannot be a key at all. That is a
    configuration error, and failing here names it — whereas deriving a key from
    garbage would surface later as "invalid signature" on every genuine delivery
    and send whoever debugs it looking at the wrong system.
    """
    if not secret:
        raise ValueError("webhook secret is empty; set LITHIC_WEBHOOK_SECRET")
    material = secret.removeprefix(SECRET_PREFIX)
    try:
        return base64.b64decode(material, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "webhook secret must be base64 after the `whsec_` prefix (see "
            "GET /v1/event_subscriptions/{token}/secret)"
        ) from exc


def signed_content(*, webhook_id: str, timestamp: str, body: bytes) -> bytes:
    return f"{webhook_id}.{timestamp}.".encode() + body


def sign(secret: str, *, webhook_id: str, timestamp: str, body: bytes) -> str:
    """One `SIGNATURE_HEADER` value, in the form Lithic sends it.

    Used by the tests and the local demo signer; a real delivery is signed by
    Lithic, and this is the code path that proves we compute what they do.
    """
    digest = hmac.new(
        signing_key(secret),
        signed_content(webhook_id=webhook_id, timestamp=timestamp, body=body),
        hashlib.sha256,
    ).digest()
    return f"{SIGNATURE_VERSION},{base64.b64encode(digest).decode()}"


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup: ASGI lower-cases names, humans do not."""
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
    """Authenticate a delivery. False for every kind of failure.

    Boolean by design: the caller answers 401 either way, and telling a caller
    which part of their forgery was closest is free help for them.
    """
    webhook_id = header_value(headers, WEBHOOK_ID_HEADER)
    timestamp = header_value(headers, TIMESTAMP_HEADER)
    signatures = header_value(headers, SIGNATURE_HEADER)
    if not webhook_id or not timestamp or not signatures:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(int(now.timestamp()) - sent_at) > tolerance_seconds:
        return False

    expected = hmac.new(
        signing_key(secret),
        signed_content(webhook_id=webhook_id, timestamp=timestamp, body=body),
        hashlib.sha256,
    ).digest()

    for candidate in signatures.split():
        version, _, encoded = candidate.partition(",")
        if version != SIGNATURE_VERSION or not encoded:
            continue
        try:
            offered = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if hmac.compare_digest(expected, offered):
            return True
    return False
