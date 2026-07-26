"""Hand-built bytes, checked against the bytes the chain accepted.

The assertion that carries this file is `test_the_built_data_is_byte_for_byte_the
_recorded_instruction`: build `transfer_native` with the same inputs the recorded
devnet transfer used, and the 55 bytes must come out identical. Everything else
here is a way for that to fail loudly rather than quietly — a transposed field, a
big-endian number, a fee where a nonce should be.

There is no Python binding for these programs, so this is the only check available
short of spending devnet SOL to find out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.chain.bridge.wormhole.accounts import transfer_accounts
from app.chain.bridge.wormhole.config import (
    SOLANA_DEVNET_CORE_BRIDGE,
    SOLANA_DEVNET_TOKEN_BRIDGE,
    WORMHOLE_CHAIN_BSC,
)
from app.chain.bridge.wormhole.instructions import (
    APPROVE_DISCRIMINATOR,
    TRANSFER_NATIVE_DISCRIMINATOR,
    approve_instruction,
    build_transfer_message,
    normalized_amount,
    populate_transaction,
    transfer_native_data,
    transfer_native_instruction,
)
from app.chain.tokens import TOKEN_PROGRAM_ID

FIXTURES = Path(__file__).parent / "fixtures" / "wormhole"

CORE = Pubkey.from_string(SOLANA_DEVNET_CORE_BRIDGE)
TOKEN_BRIDGE = Pubkey.from_string(SOLANA_DEVNET_TOKEN_BRIDGE)
BLOCKHASH = "DTdt9NMNuAaoftWPLpJNYUNqZG3Amu6Zw1NiuDsJLGUg"


def recorded_instruction() -> tuple[list[str], bytes]:
    """The token-bridge instruction from the recorded transfer: accounts and data."""
    payload: dict[str, Any] = json.loads(
        (FIXTURES / "transfer_native_transaction.json").read_text()
    )
    message = payload["result"]["transaction"]["message"]
    keys = message["accountKeys"]
    for instruction in message["instructions"]:
        if keys[instruction["programIdIndex"]] == SOLANA_DEVNET_TOKEN_BRIDGE:
            return (
                [keys[index] for index in instruction["accounts"]],
                _b58_decode(instruction["data"]),
            )
    raise AssertionError("the recorded transaction has no token-bridge instruction")


def accounts_from_recording() -> Any:
    observed, _data = recorded_instruction()
    return transfer_accounts(
        payer=Pubkey.from_string(observed[0]),
        from_token_account=Pubkey.from_string(observed[2]),
        mint=Pubkey.from_string(observed[3]),
        message=Pubkey.from_string(observed[8]),
        token_bridge_program=TOKEN_BRIDGE,
        core_bridge_program=CORE,
    )


# ------------------------------------------------------- against the chain ----


def test_the_built_data_is_byte_for_byte_the_recorded_instruction() -> None:
    _observed, recorded = recorded_instruction()

    # The recorded transfer's own values, read back out of its own bytes so this
    # is a reconstruction rather than a restatement.
    built = transfer_native_data(
        nonce=int.from_bytes(recorded[1:5], "little"),
        amount=int.from_bytes(recorded[5:13], "little"),
        relayer_fee=int.from_bytes(recorded[13:21], "little"),
        recipient=recorded[21:53],
        target_chain=int.from_bytes(recorded[53:55], "little"),
    )

    assert built == recorded
    assert len(built) == 55


def test_the_discriminator_is_the_recorded_one() -> None:
    _observed, recorded = recorded_instruction()

    assert recorded[0] == TRANSFER_NATIVE_DISCRIMINATOR == 5


def test_the_built_account_list_is_the_recorded_one() -> None:
    observed, _recorded = recorded_instruction()

    instruction = transfer_native_instruction(
        accounts=accounts_from_recording(),
        token_bridge_program=TOKEN_BRIDGE,
        nonce=0,
        amount=1,
        recipient=b"\x00" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
    )

    assert [str(meta.pubkey) for meta in instruction.accounts] == observed
    assert str(instruction.program_id) == SOLANA_DEVNET_TOKEN_BRIDGE


def test_the_recorded_approve_delegates_the_amount_to_the_authority_signer() -> None:
    # The step no documentation mentioned. Asserting it against the recording is
    # how we know the delegate is the authority_signer PDA and not the custody one.
    accounts = accounts_from_recording()
    _observed, recorded = recorded_instruction()
    amount = int.from_bytes(recorded[5:13], "little")

    approve = approve_instruction(
        source=accounts.from_token_account,
        delegate=accounts.authority_signer,
        owner=accounts.payer,
        amount=amount,
    )

    assert approve.program_id == TOKEN_PROGRAM_ID
    assert approve.data == bytes([APPROVE_DISCRIMINATOR]) + amount.to_bytes(8, "little")
    assert str(approve.accounts[1].pubkey) == str(accounts.authority_signer)
    # The owner is the only signer, and the source is the only account written.
    assert [meta.is_signer for meta in approve.accounts] == [False, False, True]
    assert [meta.is_writable for meta in approve.accounts] == [True, False, False]


# ------------------------------------------------------------ field by field ----


def test_every_number_is_little_endian() -> None:
    data = transfer_native_data(
        nonce=1,
        amount=2,
        relayer_fee=0,
        recipient=b"\xaa" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
    )

    # A big-endian encoding of these would put the significant byte at the far end
    # of each field, so this catches the whole class in one assertion.
    assert data[1:5] == b"\x01\x00\x00\x00"
    assert data[5:13] == b"\x02" + b"\x00" * 7
    assert data[53:55] == b"\x04\x00"


def test_the_recipient_must_be_thirty_two_bytes() -> None:
    # A 20-byte EVM address passed raw would shift every field after it, and the
    # bridge would read a chain id out of the middle of an address.
    with pytest.raises(ValueError, match="32 bytes"):
        transfer_native_data(
            nonce=0, amount=1, relayer_fee=0, recipient=b"\xab" * 20, target_chain=4
        )


def test_a_relayer_fee_larger_than_the_amount_is_refused() -> None:
    # The bridge would accept it and the recipient would receive nothing.
    with pytest.raises(ValueError, match="exceeds the amount"):
        transfer_native_data(
            nonce=0, amount=100, relayer_fee=101, recipient=b"\x00" * 32, target_chain=4
        )


def test_the_relayer_fee_defaults_to_zero() -> None:
    # Nothing delivers our transfers but us, so a fee would pay a relayer that
    # does not exist and reduce what arrives.
    instruction = transfer_native_instruction(
        accounts=accounts_from_recording(),
        token_bridge_program=TOKEN_BRIDGE,
        nonce=0,
        amount=1_000_000,
        recipient=b"\x11" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
    )

    assert int.from_bytes(instruction.data[13:21], "little") == 0


# --------------------------------------------------------------- normalizing ----


def test_six_decimals_pass_through_untouched() -> None:
    # USDC. Every amount this pipeline sends takes this branch.
    assert normalized_amount(1_234_567, decimals=6) == 1_234_567


def test_nine_decimals_are_truncated_the_way_the_recording_shows() -> None:
    # The recorded transfer is the vector: 625600000 at nine decimals became
    # 62560000 in the VAA.
    assert normalized_amount(625_600_000, decimals=9) == 62_560_000


def test_normalizing_truncates_rather_than_rounds() -> None:
    # Worth pinning: rounding up would promise the destination more than the
    # source locked, which the bridge would refuse.
    assert normalized_amount(999_999_999, decimals=9) == 99_999_999


def test_eight_decimals_are_the_boundary_and_pass_through() -> None:
    assert normalized_amount(12_345_678, decimals=8) == 12_345_678


# --------------------------------------------------------- the transaction ----


def test_the_message_carries_both_instructions_in_order() -> None:
    accounts = accounts_from_recording()

    message = build_transfer_message(
        accounts=accounts,
        token_bridge_program=TOKEN_BRIDGE,
        amount=1_000_000,
        recipient=b"\x22" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
        blockhash=BLOCKHASH,
    )

    # `approve` first: the bridge draws on the delegation the moment it runs.
    assert len(message.instructions) == 2
    programs = [str(message.account_keys[ix.program_id_index]) for ix in message.instructions]
    assert programs == [str(TOKEN_PROGRAM_ID), SOLANA_DEVNET_TOKEN_BRIDGE]


def test_the_payer_is_the_fee_payer_and_the_message_account_signs() -> None:
    accounts = accounts_from_recording()

    message = build_transfer_message(
        accounts=accounts,
        token_bridge_program=TOKEN_BRIDGE,
        amount=1,
        recipient=b"\x00" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
        blockhash=BLOCKHASH,
    )

    signers = {
        str(key)
        for index, key in enumerate(message.account_keys)
        if index < message.header.num_required_signatures
    }
    assert str(accounts.payer) in signers
    assert str(accounts.message) in signers
    # And nothing else does: the associated token account signs nothing.
    assert signers == {str(accounts.payer), str(accounts.message)}


def test_the_nonce_defaults_to_zero_like_the_recorded_transfer() -> None:
    _observed, recorded = recorded_instruction()
    accounts = accounts_from_recording()

    message = build_transfer_message(
        accounts=accounts,
        token_bridge_program=TOKEN_BRIDGE,
        amount=1,
        recipient=b"\x00" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
        blockhash=BLOCKHASH,
    )

    bridge_instruction = message.instructions[1]
    assert int.from_bytes(bytes(bridge_instruction.data)[1:5], "little") == 0
    assert int.from_bytes(recorded[1:5], "little") == 0


def test_signed_bytes_can_be_assembled_from_signatures_alone() -> None:
    # The split that lets a custody service provide the payer's signature: this
    # module never sees a private key.
    payer = Keypair.from_seed(bytes([8]) * 32)
    message_account = Keypair.from_seed(bytes([9]) * 32)
    accounts = transfer_accounts(
        payer=payer.pubkey(),
        from_token_account=Keypair.from_seed(bytes([10]) * 32).pubkey(),
        mint=Keypair.from_seed(bytes([11]) * 32).pubkey(),
        message=message_account.pubkey(),
        token_bridge_program=TOKEN_BRIDGE,
        core_bridge_program=CORE,
    )
    message = build_transfer_message(
        accounts=accounts,
        token_bridge_program=TOKEN_BRIDGE,
        amount=1_000_000,
        recipient=b"\x33" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
        blockhash=BLOCKHASH,
    )
    payload = bytes(message)

    signed = populate_transaction(
        message,
        [
            bytes(payer.sign_message(payload)),
            bytes(message_account.sign_message(payload)),
        ],
    )

    assert isinstance(signed, bytes)
    # Two signatures, then the message itself.
    assert signed[0] == 2
    assert payload in signed


def _b58_decode(text: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in text:
        number = number * 58 + alphabet.index(character)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw


def test_every_writable_flag_matches_the_recorded_transaction() -> None:
    # A missing `mut` aborts the transaction on-chain ("tried to write to a
    # read-only account"); a needless one on a non-program account costs nothing.
    # So the safe direction is generosity — but it turns out none is needed, and
    # pinning that keeps a future edit from loosening flags without noticing.
    payload: dict[str, Any] = json.loads(
        (FIXTURES / "transfer_native_transaction.json").read_text()
    )
    message = payload["result"]["transaction"]["message"]
    keys, header = message["accountKeys"], message["header"]
    total = len(keys)

    def recorded_writable(index: int) -> bool:
        signers = int(header["numRequiredSignatures"])
        if index < signers:
            return index < signers - int(header["numReadonlySignedAccounts"])
        return index < total - int(header["numReadonlyUnsignedAccounts"])

    indices = next(
        instruction["accounts"]
        for instruction in message["instructions"]
        if keys[instruction["programIdIndex"]] == SOLANA_DEVNET_TOKEN_BRIDGE
    )
    built = transfer_native_instruction(
        accounts=accounts_from_recording(),
        token_bridge_program=TOKEN_BRIDGE,
        nonce=0,
        amount=1,
        recipient=b"\x00" * 32,
        target_chain=WORMHOLE_CHAIN_BSC,
    )

    assert [meta.is_writable for meta in built.accounts] == [
        recorded_writable(index) for index in indices
    ]
