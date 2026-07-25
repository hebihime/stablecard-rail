"""Stripe's webhook signature scheme (phase 4).

The scheme itself is three lines of HMAC. What these tests are really for is the
*differences from Lithic*, because that is where a second implementation of "the
same" idea goes wrong:

* Stripe's signature is **hex**; Lithic's is base64.
* Stripe keys the HMAC on the secret **exactly as issued**, `whsec_` prefix and
  all; Lithic base64-decodes the part after the prefix. Same-looking secret, two
  different keys, and a wrong guess fails on every genuine delivery.
* Stripe's header is one comma-delimited `key=value` list carrying the timestamp
  *and* the signatures; Lithic spreads them across three headers.
* Stripe sends a `v0` signature on test events which is deliberately not valid.

There is no published worked example to pin this against, unlike Lithic's, so the
vectors below were computed independently of the implementation and are regression
pins rather than proof that Stripe agrees.

**The scheme itself is confirmed**, just not from here: on 2026-07-26 three genuine
deliveries forwarded by `stripe listen` verified against this code and were ledgered,
and the same deliveries returned 401 once the secret was changed
(docs/ARCHITECTURE.md §8.2). Pinning a captured delivery in the suite would be
stronger still, and is deliberately not done — verifying one needs the endpoint
secret, and committing a webhook secret to a tracked file is not a trade this project
makes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from app.issuers.stripe_issuing.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    SIGNATURE_SCHEME,
    TEST_MODE_SCHEME,
    header_value,
    sign,
    signature_header,
    signed_content,
    verify,
)

#: A secret whose part after `whsec_` is *valid base64*, so that Lithic's rule and
#: Stripe's rule both produce a key and the two can be told apart.
SECRET = "whsec_c3RhYmxlY2FyZA=="
TIMESTAMP = "1785000000"
BODY = b'{"id":"evt_1","object":"event","type":"issuing_card.created"}'
NOW = datetime.fromtimestamp(int(TIMESTAMP), tz=UTC)

#: Computed outside the implementation: HMAC-SHA256 over `"{t}.{body}"` keyed on
#: the whole secret string, hex-encoded.
EXPECTED_SIGNATURE = "73140b1da9d8a777064b87d3a4081b2feaf69b85ed0c071cc88b2ef62d8546e9"

#: The same content keyed the way *Lithic* documents — base64-decoding after the
#: prefix. A live Stripe delivery would never carry this.
LITHIC_STYLE_SIGNATURE = "aaf082aa6ea3ffe24c10b36dbc27ac1ebfde07489aba1505df2cec94707cef75"


def headers(value: str) -> dict[str, str]:
    return {"Stripe-Signature": value}


# ------------------------------------------------------------------ scheme ----


def test_the_signed_content_is_the_timestamp_a_dot_and_the_raw_body() -> None:
    assert signed_content(timestamp=TIMESTAMP, body=BODY) == TIMESTAMP.encode() + b"." + BODY


def test_the_signature_is_hex_not_base64() -> None:
    # Lithic's is base64. Getting this wrong is a silent 401 on every delivery.
    digest = sign(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert digest == EXPECTED_SIGNATURE
    assert len(digest) == 64
    bytes.fromhex(digest)


def test_the_key_is_the_whole_secret_including_the_whsec_prefix() -> None:
    # The one detail with a plausible wrong answer. Lithic documents decoding the
    # base64 after `whsec_`; Stripe documents "use the signing secret as the key"
    # and never mentions decoding, so the prefix is part of the key.
    assert (
        EXPECTED_SIGNATURE
        == hmac.new(
            SECRET.encode(), signed_content(timestamp=TIMESTAMP, body=BODY), hashlib.sha256
        ).hexdigest()
    )
    assert LITHIC_STYLE_SIGNATURE != EXPECTED_SIGNATURE


def test_a_signature_keyed_the_lithic_way_is_rejected() -> None:
    # Guards the divergence from being "tidied up" into agreement with the other
    # adapter's signing module.
    decoded = base64.b64decode(SECRET.removeprefix("whsec_"), validate=True)
    assert (
        LITHIC_STYLE_SIGNATURE
        == hmac.new(
            decoded, signed_content(timestamp=TIMESTAMP, body=BODY), hashlib.sha256
        ).hexdigest()
    )
    forged = f"t={TIMESTAMP},{SIGNATURE_SCHEME}={LITHIC_STYLE_SIGNATURE}"
    assert not verify(SECRET, headers=headers(forged), body=BODY, now=NOW)


def test_the_header_carries_the_timestamp_and_the_signature_together() -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert value == f"t={TIMESTAMP},{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE}"
    assert verify(SECRET, headers=headers(value), body=BODY, now=NOW)


def test_the_default_tolerance_is_stripes_documented_five_minutes() -> None:
    assert DEFAULT_TOLERANCE_SECONDS == 300


def test_the_header_name_is_what_asgi_will_deliver() -> None:
    assert SIGNATURE_HEADER == "stripe-signature"


# ------------------------------------------------------------------ verify ----


def test_a_genuine_delivery_verifies() -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert verify(SECRET, headers=headers(value), body=BODY, now=NOW)


def test_verification_is_case_insensitive_about_the_header_name() -> None:
    # ASGI lower-cases header names; Stripe's own docs spell it `Stripe-Signature`.
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert verify(SECRET, headers={"stripe-signature": value}, body=BODY, now=NOW)
    assert verify(SECRET, headers={"STRIPE-SIGNATURE": value}, body=BODY, now=NOW)


def test_a_body_altered_by_one_byte_fails() -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert not verify(SECRET, headers=headers(value), body=BODY + b" ", now=NOW)


def test_re_serializing_the_body_would_break_verification() -> None:
    # Why the receiver captures raw bytes: `json.loads` then `json.dumps` is a
    # different document to a signature even when it is the same object.
    import json

    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    round_tripped = json.dumps(json.loads(BODY)).encode()
    assert round_tripped != BODY
    assert not verify(SECRET, headers=headers(value), body=round_tripped, now=NOW)


def test_the_wrong_secret_fails() -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert not verify("whsec_c3RhYmxlY2FyZDI=", headers=headers(value), body=BODY, now=NOW)


def test_an_empty_secret_cannot_verify_anything() -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    assert not verify("", headers=headers(value), body=BODY, now=NOW)


def test_a_missing_header_fails() -> None:
    assert not verify(SECRET, headers={}, body=BODY, now=NOW)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "nonsense",
        f"{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE}",  # no timestamp
        f"t={TIMESTAMP}",  # no signature
        f"t=,{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE}",  # empty timestamp
        f"t=not-a-number,{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE}",
        f"t={TIMESTAMP},{SIGNATURE_SCHEME}=",  # empty signature
        f"t={TIMESTAMP},{SIGNATURE_SCHEME}=zz{EXPECTED_SIGNATURE[2:]}",  # not hex
        f"t={TIMESTAMP},{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE[:-2]}",  # truncated
    ],
    ids=[
        "empty",
        "unparseable",
        "no-timestamp",
        "no-signature",
        "blank-timestamp",
        "non-numeric-timestamp",
        "blank-signature",
        "non-hex-signature",
        "truncated-signature",
    ],
)
def test_a_malformed_header_fails_rather_than_raising(value: str) -> None:
    # Boolean by design: the receiver answers 401 either way, and an exception here
    # would turn a forgery into a 500.
    assert verify(SECRET, headers=headers(value), body=BODY, now=NOW) is False


def test_an_unknown_scheme_is_ignored_rather_than_trusted() -> None:
    value = f"t={TIMESTAMP},v9={EXPECTED_SIGNATURE}"
    assert not verify(SECRET, headers=headers(value), body=BODY, now=NOW)


def test_the_v0_test_signature_is_never_accepted() -> None:
    # Stripe sends `v0` on test events and documents it as not a valid signature.
    # Accepting it would make every Dashboard "send test webhook" click a way to
    # write to the ledger.
    value = f"t={TIMESTAMP},{TEST_MODE_SCHEME}={EXPECTED_SIGNATURE}"
    assert not verify(SECRET, headers=headers(value), body=BODY, now=NOW)


def test_several_v1_signatures_are_accepted_because_secrets_rotate() -> None:
    # Stripe keeps a rolled secret live for up to 24 hours and signs once per
    # secret, so a genuine delivery can carry a signature we cannot reproduce
    # alongside one we can.
    stale = "0" * 64
    value = f"t={TIMESTAMP},{SIGNATURE_SCHEME}={stale},{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE}"
    assert verify(SECRET, headers=headers(value), body=BODY, now=NOW)


def test_extra_unknown_fields_in_the_header_do_not_prevent_verification() -> None:
    # Forward compatibility: a new field must not read as a malformed header.
    value = (
        f"t={TIMESTAMP},{SIGNATURE_SCHEME}={EXPECTED_SIGNATURE},"
        f"{TEST_MODE_SCHEME}={'1' * 64},future=whatever"
    )
    assert verify(SECRET, headers=headers(value), body=BODY, now=NOW)


def test_whitespace_around_the_parts_is_tolerated() -> None:
    # Stripe's docs print the header across lines "for clarity"; be liberal.
    value = f"t={TIMESTAMP}, {SIGNATURE_SCHEME}={EXPECTED_SIGNATURE}"
    assert verify(SECRET, headers=headers(value), body=BODY, now=NOW)


# ------------------------------------------------------------- replay window ----


@pytest.mark.parametrize("drift", [0, 299, -299])
def test_a_delivery_inside_the_window_verifies(drift: int) -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    now = datetime.fromtimestamp(int(TIMESTAMP) + drift, tz=UTC)
    assert verify(SECRET, headers=headers(value), body=BODY, now=now)


@pytest.mark.parametrize("drift", [301, -301])
def test_a_delivery_outside_the_window_is_refused(drift: int) -> None:
    # The timestamp is inside the signed content, so an attacker replaying a
    # captured delivery cannot move it forward.
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    now = datetime.fromtimestamp(int(TIMESTAMP) + drift, tz=UTC)
    assert not verify(SECRET, headers=headers(value), body=BODY, now=now)


def test_the_window_is_configurable_per_adapter() -> None:
    value = signature_header(SECRET, timestamp=TIMESTAMP, body=BODY)
    now = datetime.fromtimestamp(int(TIMESTAMP) + 3600, tz=UTC)
    assert not verify(SECRET, headers=headers(value), body=BODY, now=now)
    assert verify(SECRET, headers=headers(value), body=BODY, now=now, tolerance_seconds=7200)


# ------------------------------------------------------------------ helpers ----


def test_header_value_finds_a_header_whatever_its_case() -> None:
    assert header_value({"Stripe-Signature": "x"}, SIGNATURE_HEADER) == "x"
    assert header_value({"stripe-signature": "x"}, SIGNATURE_HEADER) == "x"
    assert header_value({}, SIGNATURE_HEADER) is None


def test_header_value_looks_past_the_other_headers_a_delivery_carries() -> None:
    # A real delivery has a dozen headers and the signature is not the first.
    delivered = {"host": "stablecard.test", "content-type": "application/json"}
    assert header_value(delivered, SIGNATURE_HEADER) is None
    assert header_value({**delivered, "Stripe-Signature": "x"}, SIGNATURE_HEADER) == "x"
