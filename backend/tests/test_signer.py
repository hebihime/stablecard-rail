"""The signer interface, and the local keypair behind it (SPEC.md §8).

Nothing here touches a network. A signature is a pure function of a key and a
message, so these are the tests that can be exact: the same key and the same
bytes produce the same signature, and it verifies against the public key.
"""

from __future__ import annotations

import inspect
import json

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from app.chain.signer import (
    LocalKeypairSigner,
    SignerError,
    TransactionSigner,
    keypair_from_env_value,
)

#: A keypair with no funds on any network, generated for this test file. Its
#: private half is public by construction, which is the point: it can only ever
#: sign these test vectors.
TEST_KEYPAIR = Keypair.from_seed(bytes([7]) * 32)


def test_the_interface_is_a_public_key_and_a_signature() -> None:
    # Deliberately the smallest thing a custody service can honour: bytes in,
    # bytes out. An interface taking a `Transaction` could not be implemented by
    # something that has never heard of `solders` (phase 9's Fireblocks).
    assert inspect.iscoroutinefunction(TransactionSigner.sign)
    assert TransactionSigner.__abstractmethods__ == {"sign", "public_key"}


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        TransactionSigner()  # type: ignore[abstract]


async def test_a_signature_verifies_against_the_public_key() -> None:
    signer = LocalKeypairSigner(TEST_KEYPAIR)
    message = b"stablecard-rail phase 5"

    signature = await signer.sign(message)

    assert Signature.from_bytes(signature).verify(Pubkey.from_string(signer.public_key), message)


async def test_signing_is_deterministic() -> None:
    # ed25519 is deterministic, which is why a recorded signature is a usable
    # test vector at all (the same property the webhook signing tests rely on).
    signer = LocalKeypairSigner(TEST_KEYPAIR)

    assert await signer.sign(b"same bytes") == await signer.sign(b"same bytes")
    assert await signer.sign(b"same bytes") != await signer.sign(b"other bytes")


async def test_a_signature_does_not_verify_for_another_message() -> None:
    signer = LocalKeypairSigner(TEST_KEYPAIR)
    signature = await signer.sign(b"the real message")

    assert not Signature.from_bytes(signature).verify(
        Pubkey.from_string(signer.public_key), b"a different message"
    )


def test_the_public_key_is_base58_as_everything_else_sees_it() -> None:
    signer = LocalKeypairSigner(TEST_KEYPAIR)

    assert signer.public_key == str(TEST_KEYPAIR.pubkey())
    assert Pubkey.from_string(signer.public_key) == signer.pubkey


# ----------------------------------------------------- the two key formats ----


def test_a_solana_keygen_json_array_is_accepted() -> None:
    # What `solana-keygen new -o key.json` writes: 64 numbers.
    value = json.dumps(list(bytes(TEST_KEYPAIR)))

    assert keypair_from_env_value(value).pubkey() == TEST_KEYPAIR.pubkey()


def test_a_base58_secret_is_accepted() -> None:
    # What a wallet exports, and what fits on one line of a `.env` file.
    assert keypair_from_env_value(str(TEST_KEYPAIR)).pubkey() == TEST_KEYPAIR.pubkey()


def test_whitespace_around_the_value_is_tolerated() -> None:
    # `.env` files acquire trailing spaces, and the failure without this is an
    # "invalid base58" three layers from the file that caused it.
    assert keypair_from_env_value(f"  {TEST_KEYPAIR}\n  ").pubkey() == TEST_KEYPAIR.pubkey()


def test_an_empty_value_names_the_variable_to_set() -> None:
    with pytest.raises(SignerError, match="SOLANA_DEPOSIT_KEYPAIR"):
        keypair_from_env_value("   ")


@pytest.mark.parametrize(
    "value",
    ["[1, 2, 3]", "[not json", "not-base58-!!!", "abc"],
    ids=["short-array", "broken-json", "bad-base58", "too-short"],
)
def test_a_malformed_keypair_fails_at_construction_not_at_signing(value: str) -> None:
    # Eagerly, by the rule §8.11 settled: an unusable key that fails later fails
    # as "invalid signature" from the chain, which sends whoever debugs it to the
    # wrong system entirely.
    with pytest.raises(SignerError):
        keypair_from_env_value(value)


def test_from_env_value_builds_a_working_signer() -> None:
    signer = LocalKeypairSigner.from_env_value(str(TEST_KEYPAIR))

    assert signer.public_key == str(TEST_KEYPAIR.pubkey())
    assert isinstance(signer, TransactionSigner)
