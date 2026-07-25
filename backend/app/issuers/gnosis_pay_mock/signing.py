"""Gnosis Pay's webhook signature scheme.

Shaped on https://docs.gnosispay.com/webhooks/getting-started: two headers —
`x-webhook-timestamp` (unix seconds) and `x-webhook-signature` (base64) — over the
signed material `"{timestamp}.{body}"`, with a receiving window so a captured
delivery cannot be replayed indefinitely.

The scheme is **asymmetric**: Ed25519, with the verifying key published at
`PUBLIC_KEY_URL`. That is the interesting part, and it is why the mock implements
it rather than approximating it with an HMAC. A partner holds no signing secret at
all, so there is nothing on our side to leak, rotate or misconfigure — a
correctness property, not a detail of encoding. The corollary is that a `verify`
here takes a *public* key, and only the simulator ever holds the private half.

`GET https://webhooks.gnosispay.com/api/v1/public-key` answers (checked
2026-07-25, and reproduced by `public_key_document`):

    {"success":true,
     "publicKey":"-----BEGIN PUBLIC KEY-----\\nMCowBQYDK2Vw...\\n-----END PUBLIC KEY-----",
     "algorithm":"ed25519"}

Note the raw `bytes` body throughout. Verifying against re-serialized JSON is the
classic way to break every provider's signature check.

Unlike Lithic's scheme there is **no event id here to sign**, because Gnosis Pay's
envelope has none — see `GnosisPayMockAdapter.webhook_event_id`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)

SIGNATURE_HEADER = "x-webhook-signature"
TIMESTAMP_HEADER = "x-webhook-timestamp"

#: Where a partner fetches the verifying key. The mock answers the same shape from
#: `public_key_document` instead of over the network.
PUBLIC_KEY_URL = "https://webhooks.gnosispay.com/api/v1/public-key"

#: The `algorithm` field of that response.
KEY_ALGORITHM = "ed25519"

#: How far a delivery's timestamp may be from now, in either direction.
DEFAULT_TOLERANCE_SECONDS = 300

#: The mock's keypair is *derived*, not randomly generated, and this is the seed.
#: Real Gnosis Pay generates one and publishes the public half; a fresh keypair per
#: process would break the one thing the mock is for — `scripts/demo_phase2.py`
#: signs a delivery in one process and the running server verifies it in another.
#: Not a credential: the private half authenticates a simulator in this repo to
#: itself, and the whole seed is right here in the source.
MOCK_SIGNING_SEED = "gnosis-pay-mock-webhook-signing-seed"

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "KEY_ALGORITHM",
    "MOCK_SIGNING_SEED",
    "PUBLIC_KEY_URL",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "derive_signing_key",
    "header_value",
    "load_public_key",
    "public_key_document",
    "public_key_pem",
    "sign",
    "verify",
]


def derive_signing_key(seed: str = MOCK_SIGNING_SEED) -> Ed25519PrivateKey:
    """The simulator's private key, derived from `seed` so it is the same every run.

    An Ed25519 private key *is* 32 bytes of entropy, so a hash of the seed is a
    valid one; nothing here depends on it being unpredictable.
    """
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed.encode()).digest())


def public_key_pem(key: Ed25519PublicKey) -> str:
    """SPKI PEM text, which is what their public-key endpoint serves."""
    return key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


def public_key_document(key: Ed25519PublicKey) -> dict[str, Any]:
    """The body of `GET PUBLIC_KEY_URL`, field for field."""
    return {"success": True, "publicKey": public_key_pem(key), "algorithm": KEY_ALGORITHM}


def load_public_key(pem: str) -> Ed25519PublicKey:
    """Parse a published key. Raises `ValueError` on anything that is not one."""
    key = load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"expected an {KEY_ALGORITHM} public key, got {type(key).__name__}")
    return key


def _signed_material(*, timestamp: str, body: bytes) -> bytes:
    return b".".join((timestamp.encode(), body))


def sign(key: Ed25519PrivateKey, *, timestamp: str, body: bytes) -> str:
    """Produce a `SIGNATURE_HEADER` value for a delivery. The provider's side."""
    signature = key.sign(_signed_material(timestamp=timestamp, body=body))
    return base64.b64encode(signature).decode()


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
    public_key: Ed25519PublicKey,
    *,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Authenticate a delivery with the published key. Returns False on any failure.

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

    try:
        # `validate=True`: base64 quietly ignores characters outside the alphabet,
        # so without it a signature with junk spliced in could still decode.
        raw = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        return False

    try:
        public_key.verify(raw, _signed_material(timestamp=timestamp, body=body))
    except InvalidSignature:
        return False
    return True
