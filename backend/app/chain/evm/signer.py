"""Signing on the destination chain.

**Why this is not `TransactionSigner`.** The Solana interface (`app/chain/signer.py`)
is bytes in, bytes out, because that is genuinely where Solana's signing boundary
sits: a caller serializes a message and a signer signs those exact bytes. EVM's
boundary is not there. A signature goes *inside* the encoded transaction — the
recovery id depends on the chain id, and the signed payload is a keccak of an RLP
encoding that has to be rebuilt around the signature afterwards. Forcing EVM
through a bytes-in-bytes-out interface would mean reimplementing RLP here to
preserve a symmetry nothing needs, and would hand a custody service a hash with no
way to see what it is approving — the opposite of what a policy engine is for.

So: a sibling interface with the same shape and a different payload. `address`
instead of `public_key`, and a transaction instead of a message. Phase 9's
Fireblocks work fits either — custody services take a structured EVM transaction
precisely because their policy engines read the destination and the value.

**The key is a testnet key, from the environment, with no default.** Empty means
not configured, and this refuses to build rather than inventing an address nobody
funded — the same stance as `LocalKeypairSigner`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address

from app.chain.signer import SignerError

__all__ = ["EvmTransaction", "EvmTransactionSigner", "LocalPrivateKeySigner"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvmTransaction:
    """A legacy transaction, which is what actually gets mined on BSC.

    Not EIP-1559: BSC prices as a legacy chain in practice, and a type-2
    transaction there buys a `maxPriorityFeePerGas` nobody reads. `gas_price` is
    what the node suggested, and every field is an integer of wei or gas — never
    a hex string, because those are a wire format and this is not the wire.
    """

    to: str
    data: bytes
    nonce: int
    gas: int
    gas_price: int
    chain_id: int
    value: int = 0


class EvmTransactionSigner(ABC):
    """Something that can send from an EVM address.

    Async for the same reason the Solana one is: a local key does not need to be,
    and a custody service does, and the caller must not have to know which it
    holds.
    """

    @property
    @abstractmethod
    def address(self) -> str:
        """The address this signer signs for, `0x`-prefixed and checksummed."""

    @abstractmethod
    async def sign_transaction(self, transaction: EvmTransaction) -> bytes:
        """The signed, RLP-encoded transaction, ready for `eth_sendRawTransaction`."""


class LocalPrivateKeySigner(EvmTransactionSigner):
    """A key held in this process. The default, and the only one that always works."""

    def __init__(self, account: LocalAccount) -> None:
        self._account = account

    @classmethod
    def from_env_value(cls, value: str) -> LocalPrivateKeySigner:
        """Build from `EVM_REDEEMER_PRIVATE_KEY`, with or without `0x`."""
        text = value.strip()
        if not text:
            raise SignerError(
                "no redeemer key configured; set EVM_REDEEMER_PRIVATE_KEY (see .env.example)"
            )
        try:
            account: LocalAccount = Account.from_key(text)
        except (ValueError, TypeError) as exc:
            raise SignerError(f"redeemer key is not a valid private key: {exc}") from exc
        return cls(account)

    @property
    def address(self) -> str:
        return str(self._account.address)

    async def sign_transaction(self, transaction: EvmTransaction) -> bytes:
        signed = self._account.sign_transaction(
            {
                # Checksummed, because `to` arrives as a plain string from
                # configuration and eth-account's own types expect the EIP-55
                # form. It also means a mistyped address fails here rather than
                # sending funds into a plausible-looking void.
                "to": to_checksum_address(transaction.to),
                "data": transaction.data,
                "nonce": transaction.nonce,
                "gas": transaction.gas,
                "gasPrice": transaction.gas_price,
                # Signed *into* the transaction by EIP-155, which is what stops a
                # transaction being replayed on another chain — and what makes a
                # wrong chain id fail as a signature error rather than as a
                # misdelivery.
                "chainId": transaction.chain_id,
                "value": transaction.value,
            }
        )
        return bytes(signed.raw_transaction)
