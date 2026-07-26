"""The accounts a Solana token-bridge transfer touches, and how each is derived.

Seventeen accounts, eight of them program-derived. None of this is configuration:
given the two program ids, every address here follows, which is why they are
computed rather than listed. **Every derivation in this module was checked against
a real transfer on the real program** — the transaction that produced the recorded
VAA — and all eight matched on the first attempt. The test asserts that against the
recorded transaction, so a wrong seed cannot pass quietly.

Two things worth knowing before reading further, because both were discovered from
that transaction rather than from documentation:

**A transfer needs an SPL `approve` first.** The Token Bridge moves tokens with a
delegated authority: the source account's owner approves the `authority_signer` PDA
for exactly the amount, and the bridge's own instruction then draws on it. Without
the approval the transfer reverts, and nothing in the instruction layout hints that
the approval is missing.

**The message account is a signer, so it is a keypair and not a PDA.** That is the
hook this integration hangs idempotency on. Wormhole has no idempotency key — a
duplicate `submit` locks a second amount and produces a second VAA — so the message
keypair is *derived from the order reference*: a second attempt tries to create an
account that already exists, the chain refuses it, and the sequence is read back off
the existing account instead of duplicating the transfer (§10.2, point 3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.chain.bridge.base import BridgeError
from app.chain.tokens import TOKEN_PROGRAM_ID

__all__ = [
    "MESSAGE_SEED_CONTEXT",
    "POSTED_MESSAGE_MAGIC",
    "PostedMessage",
    "TransferAccounts",
    "message_keypair_from_signature",
    "message_seed_payload",
    "parse_posted_message",
    "transfer_accounts",
]

#: What a posted message account starts with. `msu` is the "unreliable" variant,
#: which the token bridge does not use but which shares the layout.
POSTED_MESSAGE_MAGIC = (b"msg\x00", b"msu\x00")

#: Domain separation for the message-keypair derivation. Prefixed so a signature
#: produced for this purpose can never be mistaken for one over a transaction.
MESSAGE_SEED_CONTEXT = b"stablecard-wormhole-message-v1:"


@dataclass(frozen=True, slots=True)
class TransferAccounts:
    """Every account `transfer_native` takes, in the order it takes them.

    The order is the instruction's ABI. It came off a real transfer rather than
    out of a header file, and `test_wormhole_accounts.py` pins it against that
    transaction.
    """

    payer: Pubkey
    config: Pubkey
    from_token_account: Pubkey
    mint: Pubkey
    custody: Pubkey
    authority_signer: Pubkey
    custody_signer: Pubkey
    core_bridge_config: Pubkey
    message: Pubkey
    emitter: Pubkey
    sequence: Pubkey
    fee_collector: Pubkey
    clock: Pubkey
    rent: Pubkey
    system_program: Pubkey
    token_program: Pubkey
    core_bridge_program: Pubkey


SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
CLOCK_SYSVAR = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")


def transfer_accounts(
    *,
    payer: Pubkey,
    from_token_account: Pubkey,
    mint: Pubkey,
    message: Pubkey,
    token_bridge_program: Pubkey,
    core_bridge_program: Pubkey,
) -> TransferAccounts:
    """Derive the whole account list from the six things a caller actually knows."""
    emitter = _pda([b"emitter"], token_bridge_program)
    return TransferAccounts(
        payer=payer,
        config=_pda([b"config"], token_bridge_program),
        from_token_account=from_token_account,
        mint=mint,
        # Seeded on the mint alone: one custody account per token, holding every
        # transfer of it that has not been redeemed elsewhere.
        custody=_pda([bytes(mint)], token_bridge_program),
        authority_signer=_pda([b"authority_signer"], token_bridge_program),
        custody_signer=_pda([b"custody_signer"], token_bridge_program),
        # Capital B. The core bridge's seeds are not the token bridge's
        # convention, and a lower-case "bridge" derives a different address that
        # exists nowhere.
        core_bridge_config=_pda([b"Bridge"], core_bridge_program),
        message=message,
        emitter=emitter,
        sequence=_pda([b"Sequence", bytes(emitter)], core_bridge_program),
        fee_collector=_pda([b"fee_collector"], core_bridge_program),
        clock=CLOCK_SYSVAR,
        rent=RENT_SYSVAR,
        system_program=SYSTEM_PROGRAM,
        token_program=TOKEN_PROGRAM_ID,
        core_bridge_program=core_bridge_program,
    )


def emitter_address(token_bridge_program: Pubkey) -> Pubkey:
    """The emitter a VAA from this Token Bridge names as its source."""
    return _pda([b"emitter"], token_bridge_program)


def _pda(seeds: list[bytes], program: Pubkey) -> Pubkey:
    address, _bump = Pubkey.find_program_address(seeds, program)
    return address


# ------------------------------------------------------- the posted message ----


@dataclass(frozen=True, slots=True)
class PostedMessage:
    """A message the core bridge has posted, as it sits in its own account.

    Read for one reason: after a duplicate `submit`, this is where the first
    attempt's sequence comes from. Every offset below was verified against the
    real account belonging to the recorded transfer — `payload_len` matched the
    VAA's payload length, `sequence` matched the VAA's sequence, `emitter_address`
    matched the emitter, and `nonce` matched the instruction that created it.
    """

    consistency_level: int
    submitted_at: int
    nonce: int
    sequence: int
    emitter_chain: int
    emitter_address: bytes
    payload: bytes


def parse_posted_message(data: bytes) -> PostedMessage:
    """Parse a posted-message account, or say why it is not one."""
    if len(data) < 95:
        raise BridgeError(f"a posted message is at least 95 bytes, got {len(data)}")
    if data[:4] not in POSTED_MESSAGE_MAGIC:
        # The check that matters: an account at this address that is *not* a
        # posted message means somebody else created it, and a transfer whose
        # message account was taken can never be posted. Better to say so than to
        # read a sequence out of unrelated bytes.
        raise BridgeError(f"account does not hold a posted message (starts {data[:4]!r})")

    payload_length = int.from_bytes(data[91:95], "little")
    return PostedMessage(
        consistency_level=data[4],
        submitted_at=int.from_bytes(data[41:45], "little"),
        nonce=int.from_bytes(data[45:49], "little"),
        sequence=int.from_bytes(data[49:57], "little"),
        emitter_chain=int.from_bytes(data[57:59], "little"),
        emitter_address=data[59:91],
        payload=data[95 : 95 + payload_length],
    )


# --------------------------------------------------- the message's identity ----


def message_seed_payload(order_ref: str) -> bytes:
    """The bytes a signer signs to derive this order's message keypair."""
    return MESSAGE_SEED_CONTEXT + order_ref.encode()


def message_keypair_from_signature(signature: bytes) -> Keypair:
    """Turn a signature over `message_seed_payload` into the message keypair.

    **Why a signature rather than a stored secret.** The keypair has to be
    reproducible from the order reference alone (that is the whole idempotency
    mechanism) *and* unguessable by anyone else — an outsider who could predict
    the address could create the account first and make the transfer permanently
    unsubmittable. A signature satisfies both without introducing a second secret
    to configure: Ed25519 signatures are deterministic by construction (RFC 8032),
    so the same signer over the same bytes always produces the same seed, and only
    the key holder can produce it.

    It also keeps the derivation inside the existing abstraction: this is
    `TransactionSigner.sign`, so a custody service (phase 9) works unchanged
    provided it will sign an arbitrary payload and signs deterministically. A
    custody service that randomizes signatures would break idempotency rather than
    correctness — worth knowing before wiring one up, and recorded in §10.4.
    """
    return Keypair.from_seed(hashlib.sha256(signature).digest())
