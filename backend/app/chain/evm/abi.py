"""Just enough ABI encoding to call three functions.

`eth-abi` is installed (it comes with `eth-account`) and would do this in one line.
It is not used, for the reason §4.3 gives about vendor SDKs, applied to a
transitive dependency: this service declares one new package for phase 6, and
reaching past it into its dependency tree makes that claim untrue. What is needed
here is a selector and three static words, and hand-encoding them means the
encoding is *checked against the chain* — `wrappedAsset` returning the right
address off BSC testnet is the proof, and it is a recorded fixture.

The three calls, all on Wormhole's Token Bridge:

* `wrappedAsset(uint16, bytes32)` — which local token stands for a foreign one.
* `isTransferCompleted(bytes32)` — has this VAA been redeemed. The authoritative
  answer to "did the money arrive", and the reason `status()` can be restarted.
* `completeTransfer(bytes)` — redeem it. The only one that writes.

Return values are read back with the same care: an address comes back as a 32-byte
word with the address in its low 20 bytes, and a `bool` as a word that is zero or
one. Neither is a string, and neither is ever `None` — a token nobody attested
answers **zero inside a 200**, which is the trap `call_wrapped_asset_unattested`
pins.
"""

from __future__ import annotations

# `eth_utils.crypto` and not `eth_utils`: the package's top level re-exports this
# without declaring it, so `mypy --strict` rejects the shorter import.
from eth_utils.crypto import keccak

__all__ = [
    "COMPLETE_TRANSFER",
    "IS_TRANSFER_COMPLETED",
    "WRAPPED_ASSET",
    "ZERO_ADDRESS",
    "decode_address",
    "decode_bool",
    "encode_bytes32_arg",
    "encode_bytes_arg",
    "encode_uint16_and_bytes32",
    "selector",
]

#: What `wrappedAsset` answers for a token nobody has attested. Not an error.
ZERO_ADDRESS = "0x" + "00" * 20

WRAPPED_ASSET = "wrappedAsset(uint16,bytes32)"
IS_TRANSFER_COMPLETED = "isTransferCompleted(bytes32)"
COMPLETE_TRANSFER = "completeTransfer(bytes)"


def selector(signature: str) -> bytes:
    """The first four bytes of the keccak-256 of a canonical function signature.

    Canonical means no argument names and no spaces — `completeTransfer(bytes)`,
    never `completeTransfer(bytes vaa)`. A wrong signature produces a valid-looking
    selector that no function answers to, and the call reverts for a reason that
    says nothing about the cause.
    """
    return keccak(text=signature)[:4]


def encode_uint16_and_bytes32(number: int, word: bytes) -> bytes:
    """Two static arguments, each padded to a 32-byte word."""
    if not 0 <= number <= 0xFFFF:
        raise ValueError(f"{number} does not fit in a uint16")
    return number.to_bytes(32, "big") + encode_bytes32_arg(word)


def encode_bytes32_arg(word: bytes) -> bytes:
    """One `bytes32`, which must already be exactly 32 bytes.

    Left-padding a short value would silently change which token or which transfer
    is being asked about, so a wrong length is an error rather than a fix-up.
    """
    if len(word) != 32:
        raise ValueError(f"a bytes32 argument is 32 bytes, got {len(word)}")
    return word


def encode_bytes_arg(payload: bytes) -> bytes:
    """One dynamic `bytes` argument: offset, length, then the data, padded.

    The offset is 32 because `bytes` is the only argument. Getting this wrong is
    how a VAA arrives at the contract as an empty array.
    """
    padding = (-len(payload)) % 32
    return (32).to_bytes(32, "big") + len(payload).to_bytes(32, "big") + payload + b"\x00" * padding


def decode_address(word: str) -> str:
    """The address in a returned 32-byte word, `0x`-prefixed and lower case."""
    raw = _one_word(word)
    return "0x" + raw[12:].hex()


def decode_bool(word: str) -> bool:
    """A returned `bool`. Anything non-zero is true, as the ABI says."""
    return any(_one_word(word))


def _one_word(word: str) -> bytes:
    raw = bytes.fromhex(word.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError(f"expected a single 32-byte word, got {len(raw)} bytes")
    return raw
