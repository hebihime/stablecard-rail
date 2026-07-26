"""Reading a real signed VAA, and refusing bytes that are not one.

The vector is not computed by the thing it tests. `vaa_token_transfer.json` is a
recorded Wormholescan response for a real transfer out of the Solana devnet Token
Bridge, and it carries the explorer's own `digest` — so the double-keccak in
`vaa.py` is checked against a hash this repository did not produce. That is the
whole point of the fixture: the hash is how the destination chain names a transfer,
and getting it wrong would make every delivered transfer look undelivered.

The suite never calls the API (SPEC.md §10).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from eth_utils.crypto import keccak

from app.chain.bridge.wormhole.vaa import (
    PAYLOAD_TRANSFER,
    MalformedVaaError,
    parse_signed_vaa,
    parse_token_transfer,
    vaa_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wormhole"

#: The Solana devnet Token Bridge's emitter. Confirmed three independent ways —
#: derived from the program, reported by Wormholescan, and answered by BSC
#: testnet's `bridgeContracts(1)`.
EMITTER_HEX = "3b26409f8aaded3f5ddca184695aa6a0fa829b0c85caf84856324896d214ca98"


def record() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / "vaa_token_transfer.json").read_text())
    data: dict[str, Any] = payload["data"]
    return data


def recorded_bytes() -> bytes:
    return base64.b64decode(record()["vaa"])


# ------------------------------------------------------------------ header ----


def test_the_recorded_vaa_parses_into_what_the_explorer_says_it_is() -> None:
    vaa = parse_signed_vaa(recorded_bytes())
    indexed = record()

    assert vaa.version == indexed["version"] == 1
    assert vaa.guardian_set_index == indexed["guardianSetIndex"]
    assert vaa.emitter_chain == indexed["emitterChain"] == 1
    assert vaa.emitter_address.hex() == indexed["emitterAddr"] == EMITTER_HEX
    assert vaa.sequence == indexed["sequence"]


def test_the_testnet_guardian_set_signs_with_one_signature() -> None:
    # Worth pinning: mainnet has nineteen guardians and testnet has one, so a
    # length assumption taken from mainnet would misplace the body by 1188 bytes.
    vaa = parse_signed_vaa(recorded_bytes())

    assert vaa.signature_count == 1
    assert len(vaa.raw) == 6 + 66 * vaa.signature_count + len(vaa.body)


def test_the_observation_time_is_utc_and_matches_the_explorer() -> None:
    vaa = parse_signed_vaa(recorded_bytes())

    assert vaa.observed_at.tzinfo is not None
    assert vaa.observed_at == datetime(2026, 7, 25, 1, 0, 38, tzinfo=UTC)
    assert vaa.observed_at.isoformat().replace("+00:00", "Z") == record()["timestamp"]


# ------------------------------------------------------------------- hash ----


def test_the_digest_is_a_double_keccak_and_the_explorer_agrees() -> None:
    # The one assertion in this file that would have been worth writing on its
    # own. `keccak256(keccak256(body))`, per Wormhole's Messages.sol — and the
    # explorer's independently computed digest for the same bytes.
    vaa = parse_signed_vaa(recorded_bytes())

    assert vaa.digest.hex() == record()["digest"]
    assert vaa.digest == keccak(keccak(vaa.body))
    assert vaa.digest != keccak(vaa.body)


def test_the_body_is_everything_after_the_signatures() -> None:
    vaa = parse_signed_vaa(recorded_bytes())

    assert vaa.body == recorded_bytes()[6 + 66 :]
    assert len(vaa.body) == 51 + len(vaa.payload)


def test_the_id_is_chain_emitter_sequence_and_matches_the_explorer() -> None:
    vaa = parse_signed_vaa(recorded_bytes())

    assert vaa.vaa_id == record()["id"]
    assert vaa.vaa_id == f"1/{EMITTER_HEX}/{vaa.sequence}"


def test_an_id_can_be_built_from_hex_or_bytes() -> None:
    # `submit` knows the emitter as bytes and the reconciler reads it back as a
    # string; both have to produce the same reference or nothing correlates.
    assert vaa_id(1, bytes.fromhex(EMITTER_HEX), 7) == vaa_id(1, EMITTER_HEX, 7)


# ---------------------------------------------------------------- payload ----


def test_the_payload_is_a_token_transfer_out_of_solana() -> None:
    transfer = parse_token_transfer(parse_signed_vaa(recorded_bytes()).payload)

    assert transfer.payload_id == PAYLOAD_TRANSFER
    assert transfer.token_chain == 1
    assert transfer.amount == 62_560_000
    # An EVM recipient lives in the low 20 bytes of a 32-byte field.
    assert transfer.to_evm_address == "0x4c587f5fc2137915c6170ad9df5b58855a2f1fd0"
    assert transfer.to[:12] == b"\x00" * 12
    assert transfer.fee == 0


def test_a_solana_token_reads_back_as_its_mint() -> None:
    transfer = parse_token_transfer(parse_signed_vaa(recorded_bytes()).payload)

    # Round-trips through base58 because a Wormhole address *is* the 32 bytes.
    assert len(transfer.token_address) == 32
    assert len(transfer.token_solana_mint) in range(32, 45)


def test_a_transfer_with_payload_has_no_fee_field() -> None:
    # Type 3 replaces the fee with a sender and an arbitrary payload. Reading a
    # fee off it would read the first four bytes of somebody's message as money.
    payload = (
        bytes([3])
        + (1_000_000).to_bytes(32, "big")
        + b"\x01" * 32
        + (1).to_bytes(2, "big")
        + b"\x02" * 32
        + (4).to_bytes(2, "big")
        + b"\x03" * 32  # sender, where a type 1 would have its fee
        + b"hello"
    )

    transfer = parse_token_transfer(payload)

    assert transfer.payload_id == 3
    assert transfer.fee is None
    assert transfer.amount == 1_000_000


# --------------------------------------------------------------- refusals ----


def test_bytes_that_are_too_short_to_be_a_header_are_refused() -> None:
    with pytest.raises(MalformedVaaError, match="at least 6 bytes"):
        parse_signed_vaa(b"\x01\x00\x00")


def test_an_unsupported_version_is_refused_rather_than_guessed_at() -> None:
    # Version is outside the signed body by design, so a future version is a
    # different format rather than a variation on this one.
    raw = bytearray(recorded_bytes())
    raw[0] = 2

    with pytest.raises(MalformedVaaError, match="version 2"):
        parse_signed_vaa(bytes(raw))


def test_a_truncated_body_is_refused() -> None:
    with pytest.raises(MalformedVaaError, match="needs"):
        parse_signed_vaa(recorded_bytes()[:80])


def test_a_signature_count_that_overruns_the_bytes_is_refused() -> None:
    # The nastiest malformed case: the count is a single byte, and a large one
    # would put the body start past the end of the array.
    raw = bytearray(recorded_bytes())
    raw[5] = 19

    with pytest.raises(MalformedVaaError, match="19 signatures"):
        parse_signed_vaa(bytes(raw))


def test_an_empty_payload_describes_no_transfer() -> None:
    with pytest.raises(MalformedVaaError, match="no payload"):
        parse_token_transfer(b"")


def test_a_payload_that_is_not_a_transfer_is_refused() -> None:
    # Type 2 is an attestation; anything else belongs to another protocol on the
    # same core bridge. Neither is money arriving.
    with pytest.raises(MalformedVaaError, match="payload type 2"):
        parse_token_transfer(bytes([2]) + b"\x00" * 200)


def test_a_transfer_payload_that_is_too_short_is_refused() -> None:
    with pytest.raises(MalformedVaaError, match="needs 133 payload bytes"):
        parse_token_transfer(bytes([1]) + b"\x00" * 100)
