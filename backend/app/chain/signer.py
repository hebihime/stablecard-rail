"""Signing (SPEC.md §8).

    `TransactionSigner` with two implementations: `LocalKeypairSigner` — devnet
    keypair from env (default; always works) — and `FireblocksSigner`. The
    interface existing is the architectural point; the Fireblocks implementation
    is the differentiator if access allows.

So the interface is deliberately the *smallest* thing both can honour: a public
key, and a signature over bytes. Everything else about a Solana transaction —
building instructions, fetching a blockhash, submitting — belongs to the caller,
because a custody service signs a payload and has no opinion about what the
payload was for.

That shape is what makes `FireblocksSigner` (phase 9) a drop-in: it is an HTTP
call that takes bytes and returns bytes, with a policy engine and an approval
flow behind it. A signer interface that took a `Transaction` object would have to
be rewritten the moment the signing happened somewhere that has never heard of
`solders`.

**The key is a devnet key and it lives in the environment.** `.env` is gitignored;
nothing here has a default, and `LocalKeypairSigner` refuses to build without one
rather than inventing a keypair whose address nobody funded.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

from solders.keypair import Keypair
from solders.pubkey import Pubkey

__all__ = ["LocalKeypairSigner", "SignerError", "TransactionSigner", "keypair_from_env_value"]

logger = logging.getLogger(__name__)


class SignerError(Exception):
    """The signer cannot be built, or cannot sign."""


class TransactionSigner(ABC):
    """Something that can prove ownership of an address.

    Both methods are async: a local keypair does not need to be, but a custody
    service does, and the caller must not have to know which it is holding.
    """

    @property
    @abstractmethod
    def public_key(self) -> str:
        """The address this signer signs for, base58, as everything else sees it."""

    @abstractmethod
    async def sign(self, message: bytes) -> bytes:
        """An ed25519 signature over exactly these bytes."""


class LocalKeypairSigner(TransactionSigner):
    """A keypair held in this process. The default, and the only one that always works."""

    def __init__(self, keypair: Keypair) -> None:
        self._keypair = keypair

    @classmethod
    def from_env_value(cls, value: str) -> LocalKeypairSigner:
        return cls(keypair_from_env_value(value))

    @property
    def public_key(self) -> str:
        return str(self._keypair.pubkey())

    @property
    def pubkey(self) -> Pubkey:
        """The `solders` form, for callers building instructions."""
        return self._keypair.pubkey()

    async def sign(self, message: bytes) -> bytes:
        return bytes(self._keypair.sign_message(message))


def keypair_from_env_value(value: str) -> Keypair:
    """A keypair from either of the two forms a devnet key comes in.

    `solana-keygen` writes a JSON array of 64 bytes; wallets and `.env` files
    carry base58. Both are accepted because both are what people actually have,
    and guessing wrong produces "invalid signature" three calls later.
    """
    text = value.strip()
    if not text:
        raise SignerError("no keypair configured; set SOLANA_DEPOSIT_KEYPAIR (see .env.example)")

    if text.startswith("["):
        try:
            numbers = json.loads(text)
            return Keypair.from_bytes(bytes(numbers))
        except (ValueError, TypeError) as exc:
            raise SignerError(f"keypair is not a valid solana-keygen JSON array: {exc}") from exc

    try:
        return Keypair.from_base58_string(text)
    except (ValueError, BaseException) as exc:  # solders raises its own error type
        raise SignerError(f"keypair is not valid base58: {exc}") from exc
