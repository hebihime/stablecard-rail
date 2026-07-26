"""Seventeen accounts, checked against the transfer the chain actually accepted.

Nothing here is computed by the thing it tests. `transfer_native_transaction.json`
is the recorded devnet transaction that produced the recorded VAA, so its account
list *is* the instruction's ABI, and every derivation in `accounts.py` has to
reproduce it exactly. A wrong seed produces a valid-looking address that exists
nowhere, and the failure arrives as a program error with nothing in it about seeds
— which is why this is asserted here rather than discovered on devnet.

The posted-message layout is checked the same way: its fields have to agree with
the VAA the guardians signed *and* with the instruction that created it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.chain.bridge.base import BridgeError
from app.chain.bridge.wormhole.accounts import (
    MESSAGE_SEED_CONTEXT,
    PostedMessage,
    emitter_address,
    message_keypair_from_signature,
    message_seed_payload,
    parse_posted_message,
    transfer_accounts,
)
from app.chain.bridge.wormhole.config import (
    SOLANA_DEVNET_CORE_BRIDGE,
    SOLANA_DEVNET_TOKEN_BRIDGE,
)
from app.chain.bridge.wormhole.vaa import parse_signed_vaa

FIXTURES = Path(__file__).parent / "fixtures" / "wormhole"

CORE = Pubkey.from_string(SOLANA_DEVNET_CORE_BRIDGE)
TOKEN_BRIDGE = Pubkey.from_string(SOLANA_DEVNET_TOKEN_BRIDGE)


def fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


def recorded_transfer() -> dict[str, Any]:
    result: dict[str, Any] = fixture("transfer_native_transaction")["result"]
    return result


def recorded_instruction() -> tuple[list[str], dict[str, Any]]:
    """The token-bridge instruction, with its accounts resolved to addresses."""
    message = recorded_transfer()["transaction"]["message"]
    keys = message["accountKeys"]
    for instruction in message["instructions"]:
        if keys[instruction["programIdIndex"]] == SOLANA_DEVNET_TOKEN_BRIDGE:
            return [keys[index] for index in instruction["accounts"]], instruction
    raise AssertionError("the recorded transaction has no token-bridge instruction")


def recorded_message_data() -> bytes:
    account = fixture("posted_message_account")["result"]["value"]
    return base64.b64decode(account["data"][0])


def recorded_vaa() -> Any:
    return parse_signed_vaa(base64.b64decode(fixture("vaa_token_transfer")["data"]["vaa"]))


# ------------------------------------------------------------- derivations ----


def test_the_derived_account_list_is_the_one_the_chain_accepted() -> None:
    observed, _instruction = recorded_instruction()

    # The three things a caller genuinely knows, read back out of the transaction
    # so that only the *derived* accounts are under test.
    accounts = transfer_accounts(
        payer=Pubkey.from_string(observed[0]),
        from_token_account=Pubkey.from_string(observed[2]),
        mint=Pubkey.from_string(observed[3]),
        message=Pubkey.from_string(observed[8]),
        token_bridge_program=TOKEN_BRIDGE,
        core_bridge_program=CORE,
    )

    assert [
        str(accounts.payer),
        str(accounts.config),
        str(accounts.from_token_account),
        str(accounts.mint),
        str(accounts.custody),
        str(accounts.authority_signer),
        str(accounts.custody_signer),
        str(accounts.core_bridge_config),
        str(accounts.message),
        str(accounts.emitter),
        str(accounts.sequence),
        str(accounts.fee_collector),
        str(accounts.clock),
        str(accounts.rent),
        str(accounts.system_program),
        str(accounts.token_program),
        str(accounts.core_bridge_program),
    ] == observed


def test_the_recorded_transfer_takes_exactly_seventeen_accounts() -> None:
    observed, _ = recorded_instruction()

    assert len(observed) == 17


def test_the_emitter_is_the_one_the_guardians_signed_for() -> None:
    # Three-way agreement: derived locally, named by the VAA, and — recorded in
    # §10.1 — answered by BSC testnet's own `bridgeContracts(1)`.
    assert bytes(emitter_address(TOKEN_BRIDGE)) == recorded_vaa().emitter_address


def test_the_core_bridge_config_seed_is_capital_b() -> None:
    # The core bridge does not follow the token bridge's naming, and a lower-case
    # "bridge" derives an address that exists nowhere. Pinned so a tidy-up
    # cannot quietly "fix" it.
    observed, _ = recorded_instruction()
    accounts = transfer_accounts(
        payer=Pubkey.from_string(observed[0]),
        from_token_account=Pubkey.from_string(observed[2]),
        mint=Pubkey.from_string(observed[3]),
        message=Pubkey.from_string(observed[8]),
        token_bridge_program=TOKEN_BRIDGE,
        core_bridge_program=CORE,
    )

    lower_case = Pubkey.find_program_address([b"bridge"], CORE)[0]
    assert str(accounts.core_bridge_config) == observed[7]
    assert accounts.core_bridge_config != lower_case


def test_the_custody_account_is_seeded_on_the_mint_alone() -> None:
    observed, _ = recorded_instruction()

    expected = Pubkey.find_program_address([bytes(Pubkey.from_string(observed[3]))], TOKEN_BRIDGE)[
        0
    ]

    assert str(expected) == observed[4]


def test_a_different_mint_has_a_different_custody_account() -> None:
    observed, _ = recorded_instruction()
    other = Keypair.from_seed(bytes([7]) * 32).pubkey()

    accounts = transfer_accounts(
        payer=Pubkey.from_string(observed[0]),
        from_token_account=Pubkey.from_string(observed[2]),
        mint=other,
        message=Pubkey.from_string(observed[8]),
        token_bridge_program=TOKEN_BRIDGE,
        core_bridge_program=CORE,
    )

    assert str(accounts.custody) != observed[4]


# ---------------------------------------------------------- posted message ----


def test_the_posted_message_agrees_with_the_vaa_the_guardians_signed() -> None:
    posted = parse_posted_message(recorded_message_data())
    vaa = recorded_vaa()

    assert posted.sequence == vaa.sequence
    assert posted.emitter_chain == vaa.emitter_chain
    assert posted.emitter_address == vaa.emitter_address
    assert posted.consistency_level == vaa.consistency_level
    # The payload the bridge posted is byte-for-byte the payload that was signed.
    assert posted.payload == vaa.payload


def test_the_posted_message_agrees_with_the_instruction_that_created_it() -> None:
    posted = parse_posted_message(recorded_message_data())
    _observed, instruction = recorded_instruction()

    # The nonce is the one field the caller chooses, so agreement here proves the
    # offsets are not accidentally reading some other field that happens to fit.
    data = _b58_decode(instruction["data"])
    assert posted.nonce == int.from_bytes(data[1:5], "little")


def test_a_posted_message_carries_a_plausible_submission_time() -> None:
    posted = parse_posted_message(recorded_message_data())

    # Seconds, not milliseconds — a millisecond value would be about 1.7e12 and
    # would read as a year in the far future.
    assert 1_600_000_000 < posted.submitted_at < 4_000_000_000


def test_an_account_that_is_not_a_posted_message_is_refused() -> None:
    # The case that matters: somebody else's account at the derived address means
    # the transfer can never be posted, and reading a sequence out of unrelated
    # bytes would invent a transfer that does not exist.
    with pytest.raises(BridgeError, match="does not hold a posted message"):
        parse_posted_message(b"\x00" * 200)


def test_a_truncated_message_account_is_refused() -> None:
    with pytest.raises(BridgeError, match="at least 95 bytes"):
        parse_posted_message(recorded_message_data()[:40])


def test_the_unreliable_message_magic_is_accepted_too() -> None:
    # `msu` shares the layout. The token bridge does not produce them, but a
    # refusal here would be a refusal on a technicality.
    data = bytearray(recorded_message_data())
    data[:4] = b"msu\x00"

    assert isinstance(parse_posted_message(bytes(data)), PostedMessage)


# ------------------------------------------------- the message's own keypair ----


def test_the_message_keypair_is_the_same_every_time_for_one_order() -> None:
    # This *is* the idempotency mechanism: same order, same account, so a second
    # submit collides on-chain instead of locking a second amount.
    signer = Keypair.from_seed(bytes([4]) * 32)
    payload = message_seed_payload("intent-1")

    first = message_keypair_from_signature(bytes(signer.sign_message(payload)))
    second = message_keypair_from_signature(bytes(signer.sign_message(payload)))

    assert first.pubkey() == second.pubkey()


def test_a_different_order_gets_a_different_message_account() -> None:
    signer = Keypair.from_seed(bytes([4]) * 32)

    one = message_keypair_from_signature(bytes(signer.sign_message(message_seed_payload("a"))))
    two = message_keypair_from_signature(bytes(signer.sign_message(message_seed_payload("b"))))

    assert one.pubkey() != two.pubkey()


def test_a_different_signer_gets_a_different_message_account() -> None:
    # Why a signature and not a hash of the order id: an outsider who could
    # predict the address could create the account first and make the transfer
    # permanently unsubmittable.
    payload = message_seed_payload("intent-1")
    ours = Keypair.from_seed(bytes([4]) * 32)
    theirs = Keypair.from_seed(bytes([5]) * 32)

    assert (
        message_keypair_from_signature(bytes(ours.sign_message(payload))).pubkey()
        != message_keypair_from_signature(bytes(theirs.sign_message(payload))).pubkey()
    )


def test_the_seed_payload_is_domain_separated() -> None:
    # So a signature produced to derive a message account can never be mistaken
    # for a signature over a transaction.
    payload = message_seed_payload("intent-1")

    assert payload.startswith(MESSAGE_SEED_CONTEXT)
    assert payload.endswith(b"intent-1")


def _b58_decode(text: str) -> bytes:
    """Base58, without a dependency for four lines of arithmetic."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in text:
        number = number * 58 + alphabet.index(character)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
