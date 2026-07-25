"""Lithic's webhook signature scheme (SPEC.md §3.2).

Verification is the one part of an adapter where being *nearly* right is worse than
being absent: it is the only thing standing between the internet and the ledger. So
these tests are written against Lithic's published scheme rather than against our
implementation of it, and the first of them is their own documented worked example —
a vector we did not choose, computed by code we did not write.

The details that are easy to get wrong, and each has a test here:

* the HMAC key is the base64-**decoded** portion after `whsec_`, not that string;
* the signed content covers the `webhook-id`, which is also the dedup key — so a
  replay cannot be relabelled as a new event;
* `webhook-signature` may carry several space-delimited signatures during a secret
  rotation, and any one of them matching is a pass;
* the body must be the raw bytes. Re-serialized JSON has different bytes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from app.issuers.lithic.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    SIGNATURE_VERSION,
    TIMESTAMP_HEADER,
    WEBHOOK_ID_HEADER,
    header_value,
    sign,
    signing_key,
    verify,
)

# --- Lithic's documented example (docs.lithic.com "Example signature computation")
GOLDEN_ID = "65a9dad4-1b60-4686-83fd-65b25078a4b4"
GOLDEN_TIMESTAMP = "1698031907"  # 2023-10-23 03:31:47 UTC
GOLDEN_SECRET = "aDeFC3Zn55XB3PDD2zF0JP9cyrDHdV/18VOmkTcuyto="
GOLDEN_BODY = b'{"acquirer_fee":0,"amount":2000,"authorization_amount":2000}'
GOLDEN_SIGNATURE = "OGBiqPtc/O2sWacUsuS4pvTdfFBv6dqxYX/4UFzrbGk="
GOLDEN_NOW = datetime(2023, 10, 23, 3, 31, 47, tzinfo=UTC)

SECRET = "whsec_" + base64.b64encode(b"phase-3-test-key-material-32byte").decode()
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
BODY = b'{"card_token":"c0ffee","event_type":"card.created"}'
EVENT_ID = "msg_2mockmockmockmockmockmock"


def headers_for(
    *,
    secret: str = SECRET,
    event_id: str = EVENT_ID,
    body: bytes = BODY,
    now: datetime = NOW,
    signature: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(now.timestamp()))
    return {
        WEBHOOK_ID_HEADER: event_id,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature
        if signature is not None
        else sign(secret, webhook_id=event_id, timestamp=timestamp, body=body),
    }


# ------------------------------------------------------------ golden vector ----


def test_lithics_own_worked_example_verifies() -> None:
    # If this fails, the scheme is wrong — not the test. Their example gives the
    # secret already stripped of `whsec_`, which is also the form we must accept.
    headers = {
        WEBHOOK_ID_HEADER: GOLDEN_ID,
        TIMESTAMP_HEADER: GOLDEN_TIMESTAMP,
        SIGNATURE_HEADER: f"{SIGNATURE_VERSION},{GOLDEN_SIGNATURE}",
    }
    assert verify(GOLDEN_SECRET, headers=headers, body=GOLDEN_BODY, now=GOLDEN_NOW)


def test_we_produce_the_signature_from_lithics_example_ourselves() -> None:
    produced = sign(
        GOLDEN_SECRET, webhook_id=GOLDEN_ID, timestamp=GOLDEN_TIMESTAMP, body=GOLDEN_BODY
    )
    assert f"{SIGNATURE_VERSION},{GOLDEN_SIGNATURE}" == produced


def test_the_key_is_the_decoded_secret_not_the_string() -> None:
    # The single most likely way to get this wrong: HMAC over the base64 text
    # instead of the bytes it encodes. Both "work" until a real delivery arrives.
    assert base64.b64decode(GOLDEN_SECRET) == signing_key(GOLDEN_SECRET)
    wrong = hmac.new(
        GOLDEN_SECRET.encode(),
        f"{GOLDEN_ID}.{GOLDEN_TIMESTAMP}.".encode() + GOLDEN_BODY,
        hashlib.sha256,
    ).digest()
    assert GOLDEN_SIGNATURE != base64.b64encode(wrong).decode()


def test_the_whsec_prefix_is_optional_and_makes_no_difference() -> None:
    bare = GOLDEN_SECRET
    prefixed = f"whsec_{GOLDEN_SECRET}"
    assert signing_key(prefixed) == signing_key(bare)


# -------------------------------------------------------------- happy paths ----


def test_a_signature_we_produce_is_one_we_accept() -> None:
    assert verify(SECRET, headers=headers_for(), body=BODY, now=NOW)


def test_header_lookup_is_case_insensitive() -> None:
    # ASGI lower-cases header names; a curl user or a hand-written test does not.
    headers = {name.upper(): value for name, value in headers_for().items()}
    assert verify(SECRET, headers=headers, body=BODY, now=NOW)
    assert EVENT_ID == header_value(headers, WEBHOOK_ID_HEADER)


def test_a_missing_header_is_not_a_lookup_error() -> None:
    assert header_value({}, WEBHOOK_ID_HEADER) is None


def test_any_one_of_several_rotated_signatures_is_enough() -> None:
    # During a secret rotation Lithic signs with both keys and sends both values.
    good = sign(SECRET, webhook_id=EVENT_ID, timestamp=str(int(NOW.timestamp())), body=BODY)
    stale = f"{SIGNATURE_VERSION},{base64.b64encode(b'x' * 32).decode()}"
    for combined in (f"{stale} {good}", f"{good} {stale}"):
        headers = headers_for(signature=combined)
        assert verify(SECRET, headers=headers, body=BODY, now=NOW), combined


def test_extra_whitespace_between_signatures_is_tolerated() -> None:
    good = sign(SECRET, webhook_id=EVENT_ID, timestamp=str(int(NOW.timestamp())), body=BODY)
    assert verify(SECRET, headers=headers_for(signature=f"  {good}  "), body=BODY, now=NOW)


# ------------------------------------------------------------- rejections ----


@pytest.mark.parametrize("dropped", (WEBHOOK_ID_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER))
def test_a_delivery_missing_any_verification_header_is_rejected(dropped: str) -> None:
    headers = headers_for()
    del headers[dropped]
    assert not verify(SECRET, headers=headers, body=BODY, now=NOW)


def test_a_body_changed_in_transit_is_rejected() -> None:
    assert not verify(SECRET, headers=headers_for(), body=BODY + b" ", now=NOW)


def test_a_rewritten_event_id_is_rejected() -> None:
    # The id is inside the signed content, so an attacker cannot re-send a genuine
    # body under a fresh id to slip past the dedup gate (SPEC.md §4).
    headers = headers_for()
    headers[WEBHOOK_ID_HEADER] = "msg_attacker_chosen"
    assert not verify(SECRET, headers=headers, body=BODY, now=NOW)


def test_a_rewritten_timestamp_is_rejected() -> None:
    headers = headers_for()
    headers[TIMESTAMP_HEADER] = str(int(NOW.timestamp()) + 1)
    assert not verify(SECRET, headers=headers, body=BODY, now=NOW)


def test_another_programs_secret_does_not_verify() -> None:
    other = "whsec_" + base64.b64encode(b"a-different-programs-key-32bytes").decode()
    assert not verify(other, headers=headers_for(), body=BODY, now=NOW)


@pytest.mark.parametrize(
    "signature",
    ["", "not-a-signature", "v2,abcd", f"{SIGNATURE_VERSION},", "v1", ","],
    ids=["empty", "shapeless", "future-version", "no-digest", "no-comma", "bare-comma"],
)
def test_a_malformed_signature_header_is_rejected(signature: str) -> None:
    assert not verify(SECRET, headers=headers_for(signature=signature), body=BODY, now=NOW)


def test_a_digest_that_is_not_base64_is_rejected_rather_than_raised() -> None:
    headers = headers_for(signature="v1,!!!not base64!!!")
    assert not verify(SECRET, headers=headers, body=BODY, now=NOW)


@pytest.mark.parametrize("timestamp", ["", "not-a-number", "1698031907.5", "0x10"])
def test_a_timestamp_that_is_not_unix_seconds_is_rejected(timestamp: str) -> None:
    headers = headers_for()
    headers[TIMESTAMP_HEADER] = timestamp
    assert not verify(SECRET, headers=headers, body=BODY, now=NOW)


def test_a_secret_that_is_not_base64_is_a_configuration_error() -> None:
    # Refuse at the boundary rather than verifying every delivery against a key
    # derived from garbage — that would fail as "bad signature" and send whoever
    # debugs it looking in the wrong place entirely.
    with pytest.raises(ValueError, match="base64"):
        signing_key("whsec_this is not base64 at all!!")


def test_an_empty_secret_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="empty"):
        signing_key("")


# --------------------------------------------------------------- freshness ----


@pytest.mark.parametrize("drift", (-1, 1))
def test_a_delivery_just_inside_the_tolerance_is_accepted(drift: int) -> None:
    sent_at = NOW + timedelta(seconds=drift * (DEFAULT_TOLERANCE_SECONDS - 1))
    assert verify(SECRET, headers=headers_for(now=sent_at), body=BODY, now=NOW)


@pytest.mark.parametrize("drift", (-1, 1))
def test_a_delivery_outside_the_tolerance_is_rejected(drift: int) -> None:
    # Both directions: a captured delivery replayed later, and one dated in the
    # future to outlive the window.
    sent_at = NOW + timedelta(seconds=drift * (DEFAULT_TOLERANCE_SECONDS + 1))
    assert not verify(SECRET, headers=headers_for(now=sent_at), body=BODY, now=NOW)


def test_the_tolerance_is_configurable() -> None:
    sent_at = NOW - timedelta(seconds=DEFAULT_TOLERANCE_SECONDS + 60)
    headers = headers_for(now=sent_at)
    assert not verify(SECRET, headers=headers, body=BODY, now=NOW)
    assert verify(SECRET, headers=headers, body=BODY, now=NOW, tolerance_seconds=3_600)


def test_the_default_tolerance_matches_the_documented_recommendation() -> None:
    assert 300 == DEFAULT_TOLERANCE_SECONDS
