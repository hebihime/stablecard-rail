"""Where an SPL token lands: the associated token account (SPEC.md §9.3).

A Solana wallet address does not hold USDC. A *token account* does, and the one a
wallet holds a given mint in is derived — same owner, same mint, same address,
every time, without asking a node. That derived address is the "Solana devnet
deposit address" the fund screen shows and the watcher polls (§9.8's *source*
address), so this is what turns a keypair into something a deposit can be sent to.

Derivation only. Creating the account, and transferring into it, belong to whoever
sends — the in-app wallet in phase 8 — and the watcher does not care how the money
arrived (§9.6).
"""

from __future__ import annotations

from solders.pubkey import Pubkey

__all__ = [
    "ASSOCIATED_TOKEN_PROGRAM_ID",
    "TOKEN_PROGRAM_ID",
    "associated_token_address",
    "base58_to_bytes32",
]

#: The SPL Token program, and the program that derives accounts for it. Constants
#: of the network rather than configuration: a different value is a different
#: chain, not a different deployment.
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """The account `owner` holds `mint` in.

    A program-derived address over `[owner, token program, mint]`. Being a PDA it
    is off the ed25519 curve, so no keypair signs for it directly — the owner
    authorises transfers out of it, which is why the *owner* is what the recorded
    deposit's `postTokenBalances` names.
    """
    address, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return address


def base58_to_bytes32(address: str) -> bytes:
    """A Solana address as the 32 raw bytes another chain refers to it by.

    Wormhole addresses every chain in 32 bytes, which a Solana pubkey already is —
    so this is a decoding, not a hash, and it round-trips. It exists because a
    contract on an EVM chain has never heard of base58: `wrappedAsset(1, mint)`
    takes the mint as `bytes32` (docs/ARCHITECTURE.md §10.1).
    """
    return bytes(Pubkey.from_string(address))
