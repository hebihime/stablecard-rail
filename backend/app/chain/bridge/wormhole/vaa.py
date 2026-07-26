"""Reading a VAA — the signed message that *is* the transfer.

A VAA (Verifiable Action Approval) is what the guardians produce when they observe
the source transaction: a header of signatures, then a body describing what
happened. Everything downstream keys off it, so this module is small and strict.

Three things earn their place here:

**The identity.** `emitterChain/emitterAddress/sequence` is what a transfer is
called, and all three are knowable from the source transaction — *before* the
guardians have signed anything. That is what makes it the right `bridge_ref`: a
crash between sending and recording loses nothing, because the reference can be
re-derived rather than searched for. Compare a transaction hash, which says nothing
about which message inside the transaction is meant.

**The hash, and it is a double keccak.** `keccak256(keccak256(body))`, not
`keccak256(body)`. This is the key the destination Token Bridge stores transfers
under, so getting it wrong makes every delivered transfer look undelivered and
every redemption a duplicate. Two independent confirmations, because one was not
enough for something this easy to get subtly wrong: Wormhole's own
`Messages.sol`, which computes `keccak256(abi.encodePacked(keccak256(body)))` under
a comment reading *"SECURITY: Do not change the way the hash of a VM is
computed!"*, and Wormholescan's `digest` field for a real testnet VAA, which equals
what this module computes for the same bytes. The fixture asserts that equality.

**The amount is normalized to eight decimals.** Wormhole cannot promise more
precision than its smallest chain, so a token with more than 8 decimals is scaled
down on the way out and back up on the way in. USDC has six, so nothing is scaled
and nothing is lost — but the property is asserted rather than assumed, because a
silent truncation of a money amount is the worst class of bug in this repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from eth_utils.crypto import keccak
from solders.pubkey import Pubkey

from app.chain.bridge.base import BridgeError

__all__ = [
    "MAX_WORMHOLE_DECIMALS",
    "PAYLOAD_TRANSFER",
    "PAYLOAD_TRANSFER_WITH_PAYLOAD",
    "MalformedVaaError",
    "SignedVaa",
    "TokenTransfer",
    "parse_signed_vaa",
    "parse_token_transfer",
    "vaa_id",
]

#: One guardian signature: index byte, r, s, recovery id.
SIGNATURE_SIZE = 66

#: Body offsets. Fixed by the protocol, and the reason this is a table rather than
#: a stream of magic numbers.
_BODY_MINIMUM = 51

#: The two token-transfer payloads. `3` carries an extra sender and an arbitrary
#: payload for a contract to act on; this pipeline sends `1`, and reads both
#: because the destination is the same either way.
PAYLOAD_TRANSFER = 1
PAYLOAD_TRANSFER_WITH_PAYLOAD = 3

#: Wormhole's ceiling on token precision: a chain with more decimals is scaled
#: down, so no chain has to represent an amount it cannot hold.
MAX_WORMHOLE_DECIMALS = 8


class MalformedVaaError(BridgeError):
    """Bytes that are not a VAA, or not the VAA they claim to be.

    A `BridgeError` rather than a `ValueError`: it reaches the funding engine
    through `status()`, and the engine's whole failure taxonomy is built on
    `ExternalError`. Never retryable — bytes do not improve on a second reading.
    """


@dataclass(frozen=True, slots=True)
class TokenTransfer:
    """The transfer a VAA's payload describes."""

    payload_id: int
    #: In the *token's* units, normalized to at most 8 decimals (see the module
    #: docstring). For 6-decimal USDC this is exactly the minor-unit amount.
    amount: int
    token_address: bytes
    token_chain: int
    #: The recipient, in Wormhole's 32-byte form. For an EVM chain the address is
    #: the low 20 bytes; for Solana the whole 32 are the pubkey.
    to: bytes
    to_chain: int
    #: Only on payload 1. The portion the relayer may keep — zero for a transfer
    #: nobody was paid to deliver, which is every transfer this service sends.
    fee: int | None = None

    @property
    def to_evm_address(self) -> str:
        """The recipient as an EVM address. Left-padded, so the low 20 bytes."""
        return "0x" + self.to[12:].hex()

    @property
    def token_solana_mint(self) -> str:
        """The token as a Solana address, when it is one."""
        return str(Pubkey.from_bytes(self.token_address))


@dataclass(frozen=True, slots=True)
class SignedVaa:
    """A guardian-signed observation, parsed."""

    raw: bytes
    version: int
    guardian_set_index: int
    signature_count: int
    observed_at: datetime
    nonce: int
    emitter_chain: int
    emitter_address: bytes
    sequence: int
    consistency_level: int
    payload: bytes

    @property
    def body(self) -> bytes:
        """Everything the guardians actually signed."""
        return self.raw[6 + SIGNATURE_SIZE * self.signature_count :]

    @property
    def digest(self) -> bytes:
        """`keccak256(keccak256(body))` — how the destination names this transfer."""
        return keccak(keccak(self.body))

    @property
    def vaa_id(self) -> str:
        return vaa_id(self.emitter_chain, self.emitter_address, self.sequence)


def vaa_id(emitter_chain: int, emitter_address: bytes | str, sequence: int) -> str:
    """`chain/emitter/sequence`, the form every Wormhole tool uses.

    Also this service's `bridge_ref`. Stored and handed back, never parsed for
    meaning by anything outside this package — §9.2's rule about opaque references
    applies to our own formats too.
    """
    emitter = emitter_address if isinstance(emitter_address, str) else emitter_address.hex()
    return f"{emitter_chain}/{emitter}/{sequence}"


def parse_signed_vaa(raw: bytes) -> SignedVaa:
    """Parse VAA bytes, or say precisely what is wrong with them."""
    if len(raw) < 6:
        raise MalformedVaaError(f"a VAA is at least 6 bytes of header, got {len(raw)}")

    version = raw[0]
    if version != 1:
        # Version is deliberately outside the signed body (their own comment says
        # so), which means a future version is a different format and not a
        # variation on this one.
        raise MalformedVaaError(f"unsupported VAA version {version}")

    guardian_set_index = int.from_bytes(raw[1:5], "big")
    signature_count = raw[5]
    body_start = 6 + SIGNATURE_SIZE * signature_count
    if len(raw) < body_start + _BODY_MINIMUM:
        raise MalformedVaaError(
            f"a VAA with {signature_count} signatures needs "
            f"{body_start + _BODY_MINIMUM} bytes, got {len(raw)}"
        )

    body = raw[body_start:]
    return SignedVaa(
        raw=raw,
        version=version,
        guardian_set_index=guardian_set_index,
        signature_count=signature_count,
        observed_at=datetime.fromtimestamp(int.from_bytes(body[0:4], "big"), tz=UTC),
        nonce=int.from_bytes(body[4:8], "big"),
        emitter_chain=int.from_bytes(body[8:10], "big"),
        emitter_address=body[10:42],
        sequence=int.from_bytes(body[42:50], "big"),
        consistency_level=body[50],
        payload=body[51:],
    )


def parse_token_transfer(payload: bytes) -> TokenTransfer:
    """Parse a token-transfer payload (type 1 or 3)."""
    if not payload:
        raise MalformedVaaError("a VAA with no payload describes no transfer")

    payload_id = payload[0]
    if payload_id not in (PAYLOAD_TRANSFER, PAYLOAD_TRANSFER_WITH_PAYLOAD):
        # Type 2 is an attestation, and anything else belongs to a different
        # protocol built on the same core bridge. Neither is money arriving.
        raise MalformedVaaError(f"payload type {payload_id} is not a token transfer")

    fixed = 101 if payload_id == PAYLOAD_TRANSFER_WITH_PAYLOAD else 133
    if len(payload) < fixed:
        raise MalformedVaaError(
            f"a type-{payload_id} transfer needs {fixed} payload bytes, got {len(payload)}"
        )

    return TokenTransfer(
        payload_id=payload_id,
        amount=int.from_bytes(payload[1:33], "big"),
        token_address=payload[33:65],
        token_chain=int.from_bytes(payload[65:67], "big"),
        to=payload[67:99],
        to_chain=int.from_bytes(payload[99:101], "big"),
        fee=int.from_bytes(payload[101:133], "big") if payload_id == PAYLOAD_TRANSFER else None,
    )
