"""Deriving a deposit address, checked against a real devnet account.

The vector here is not computed by the thing it tests. `transaction_transfer_checked`
is a recorded devnet transfer, and its `postTokenBalances` name both the token
account that was credited *and* the wallet that owns it. Deriving one from the
other has to reproduce what the chain already did, which is the only kind of test
of a derivation that means anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.chain.config import USDC_DEVNET_MINT
from app.chain.tokens import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    associated_token_address,
)

FIXTURES = Path(__file__).parent / "fixtures" / "solana"

#: The credited account and its owner, both read out of the recorded transfer.
RECORDED_TOKEN_ACCOUNT = "GXGc5RJU7W4j8FrH38vfGbryht5av3zeiZCmhDN7yRPU"
RECORDED_OWNER = "D7d6ZP2KcmHzhEh4nGrpe7zMKFjXP8wtvceecGB3fKVp"


def recorded_transfer() -> Any:
    return json.loads((FIXTURES / "transaction_transfer_checked.json").read_text())["result"]


def test_the_fixture_really_pairs_that_owner_with_that_account() -> None:
    # Guard on the vector itself: if the fixture is re-recorded against a
    # different transfer, this fails loudly instead of the test below passing
    # against constants nobody checked.
    transaction = recorded_transfer()
    keys = [key["pubkey"] for key in transaction["transaction"]["message"]["accountKeys"]]
    credited = next(
        balance
        for balance in transaction["meta"]["postTokenBalances"]
        if keys[balance["accountIndex"]] == RECORDED_TOKEN_ACCOUNT
    )

    assert credited["owner"] == RECORDED_OWNER
    assert credited["mint"] == USDC_DEVNET_MINT


def test_derivation_reproduces_the_account_the_chain_used() -> None:
    derived = associated_token_address(
        Pubkey.from_string(RECORDED_OWNER),
        Pubkey.from_string(USDC_DEVNET_MINT),
    )

    assert str(derived) == RECORDED_TOKEN_ACCOUNT


def test_a_different_mint_is_a_different_account() -> None:
    owner = Pubkey.from_string(RECORDED_OWNER)
    mainnet_usdc = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

    assert associated_token_address(
        owner, Pubkey.from_string(USDC_DEVNET_MINT)
    ) != associated_token_address(owner, mainnet_usdc)


def test_a_different_owner_is_a_different_account() -> None:
    mint = Pubkey.from_string(USDC_DEVNET_MINT)
    stranger = Keypair.from_seed(bytes([9]) * 32).pubkey()

    assert associated_token_address(
        Pubkey.from_string(RECORDED_OWNER), mint
    ) != associated_token_address(stranger, mint)


def test_derivation_is_stable() -> None:
    # The whole point: a deposit address a wallet can be shown, and a watcher can
    # poll, without asking a node anything.
    owner = Keypair.from_seed(bytes([3]) * 32).pubkey()
    mint = Pubkey.from_string(USDC_DEVNET_MINT)

    assert associated_token_address(owner, mint) == associated_token_address(owner, mint)


def test_the_program_ids_are_the_ones_the_recorded_transaction_names() -> None:
    # Not configuration: a different value here is a different chain. The
    # recorded transfer's own account list is the evidence.
    keys = [key["pubkey"] for key in recorded_transfer()["transaction"]["message"]["accountKeys"]]

    assert str(TOKEN_PROGRAM_ID) in keys
    assert str(ASSOCIATED_TOKEN_PROGRAM_ID) in keys
