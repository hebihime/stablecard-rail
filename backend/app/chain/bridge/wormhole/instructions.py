"""Building the two instructions a transfer needs, and the transaction around them.

There is no Python binding for Wormhole's Solana programs — the SDK is TypeScript
— so these bytes are hand-built. That is only safe because the layout came off a
transaction the chain accepted, and `test_wormhole_instructions.py` asserts that
what this module builds is **byte-for-byte** what that transaction carried. A
hand-built instruction that is wrong fails on devnet with a program error saying
nothing about which field is off, so the assertion is the difference between
knowing and hoping.

**Two instructions, in this order:**

1. `approve` on the SPL Token program, delegating exactly `amount` to the Token
   Bridge's `authority_signer` PDA. Discovered from the recording, not from
   documentation — without it the transfer reverts.
2. `transfer_native` on the Token Bridge: 55 bytes, discriminator `5`, then nonce,
   amount, fee, the 32-byte recipient and the Wormhole chain id, all little-endian
   Borsh.

**The amount is the token's own, not the VAA's.** The instruction takes six-decimal
USDC as six-decimal USDC; Wormhole normalizes to eight decimals on the way out, so
the VAA that comes back carries the same number for USDC and a *smaller* one for a
nine-decimal token. `normalized_amount` exists to make that explicit rather than
surprising, and `MAX_WORMHOLE_DECIMALS` is where the rule lives.

**The relayer fee is zero, always.** It is the slice a third party may keep for
delivering the transfer, and nothing delivers ours but us (§10.2, point 1). A
non-zero fee here would pay a relayer that does not exist and reduce what arrives.
"""

from __future__ import annotations

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from app.chain.bridge.wormhole.accounts import TransferAccounts
from app.chain.bridge.wormhole.vaa import MAX_WORMHOLE_DECIMALS
from app.chain.tokens import TOKEN_PROGRAM_ID

__all__ = [
    "APPROVE_DISCRIMINATOR",
    "TRANSFER_NATIVE_DISCRIMINATOR",
    "approve_instruction",
    "build_transfer_message",
    "normalized_amount",
    "populate_transaction",
    "transfer_native_data",
    "transfer_native_instruction",
]

#: SPL Token's `Approve`. A single-byte discriminator, then a u64 amount.
APPROVE_DISCRIMINATOR = 4

#: The Token Bridge's `TransferNative`. Read off the recorded instruction's first
#: byte, and pinned by its test — the enum's order is not something to infer.
TRANSFER_NATIVE_DISCRIMINATOR = 5


def normalized_amount(amount: int, *, decimals: int) -> int:
    """What a VAA will report for `amount` of a token with `decimals`.

    Wormhole cannot promise more precision than its least precise chain, so
    anything beyond eight decimals is **truncated** — not rounded — on the way
    out. USDC has six, so this is the identity for every amount this pipeline
    sends, and the function exists so that the day it is not, the truncation is a
    named thing rather than a surprise in a reconciliation report.
    """
    if decimals <= MAX_WORMHOLE_DECIMALS:
        return amount
    scale: int = 10 ** (decimals - MAX_WORMHOLE_DECIMALS)
    return amount // scale


def transfer_native_data(
    *, nonce: int, amount: int, relayer_fee: int, recipient: bytes, target_chain: int
) -> bytes:
    """The 55 bytes of `transfer_native`, little-endian throughout."""
    if len(recipient) != 32:
        raise ValueError(f"a Wormhole recipient is 32 bytes, got {len(recipient)}")
    if relayer_fee > amount:
        # The bridge would accept it and the recipient would receive nothing.
        raise ValueError(f"a relayer fee of {relayer_fee} exceeds the amount {amount}")

    return (
        bytes([TRANSFER_NATIVE_DISCRIMINATOR])
        + nonce.to_bytes(4, "little")
        + amount.to_bytes(8, "little")
        + relayer_fee.to_bytes(8, "little")
        + recipient
        + target_chain.to_bytes(2, "little")
    )


def approve_instruction(
    *, source: Pubkey, delegate: Pubkey, owner: Pubkey, amount: int
) -> Instruction:
    """Delegate `amount` from `source` to the Token Bridge's authority.

    The step the instruction layout does not hint at. `owner` signs, `source` is
    written, and the delegation is for exactly the transfer amount so nothing is
    left standing afterwards.
    """
    return Instruction(
        program_id=TOKEN_PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=source, is_signer=False, is_writable=True),
            AccountMeta(pubkey=delegate, is_signer=False, is_writable=False),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ],
        data=bytes([APPROVE_DISCRIMINATOR]) + amount.to_bytes(8, "little"),
    )


def transfer_native_instruction(
    *,
    accounts: TransferAccounts,
    token_bridge_program: Pubkey,
    nonce: int,
    amount: int,
    recipient: bytes,
    target_chain: int,
    relayer_fee: int = 0,
) -> Instruction:
    """`transfer_native`, with its seventeen accounts in the order it wants them.

    The writable and signer flags mirror the recorded transaction. Two notes on
    reading those flags off a recording, because the mapping is not exact:

    * A transaction's flags are the **union** across its instructions, so an
      account can look writable because a *different* instruction wrote it. Being
      generous is safe (a needless `mut` on a non-program account costs nothing);
      being stingy is fatal, since a program that writes a read-only account
      aborts the transaction.
    * `from_token_account` was a **signer** in the recording, which is incidental:
      that transfer used a temporary wrapped-SOL account whose keypair had to sign
      its own creation. An associated token account signs nothing, and the Token
      Bridge does not ask it to — the `approve` above is what authorises the move.
    """
    return Instruction(
        program_id=token_bridge_program,
        accounts=[
            AccountMeta(pubkey=accounts.payer, is_signer=True, is_writable=True),
            AccountMeta(pubkey=accounts.config, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.from_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=accounts.mint, is_signer=False, is_writable=True),
            AccountMeta(pubkey=accounts.custody, is_signer=False, is_writable=True),
            AccountMeta(pubkey=accounts.authority_signer, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.custody_signer, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.core_bridge_config, is_signer=False, is_writable=True),
            # The message account signs, which is why it is a keypair and why
            # deriving it from the order reference gives idempotency.
            AccountMeta(pubkey=accounts.message, is_signer=True, is_writable=True),
            AccountMeta(pubkey=accounts.emitter, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.sequence, is_signer=False, is_writable=True),
            AccountMeta(pubkey=accounts.fee_collector, is_signer=False, is_writable=True),
            AccountMeta(pubkey=accounts.clock, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.rent, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.system_program, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.token_program, is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts.core_bridge_program, is_signer=False, is_writable=False),
        ],
        data=transfer_native_data(
            nonce=nonce,
            amount=amount,
            relayer_fee=relayer_fee,
            recipient=recipient,
            target_chain=target_chain,
        ),
    )


def build_transfer_message(
    *,
    accounts: TransferAccounts,
    token_bridge_program: Pubkey,
    amount: int,
    recipient: bytes,
    target_chain: int,
    blockhash: str,
    nonce: int = 0,
) -> Message:
    """Both instructions, in order, against a recent blockhash.

    The nonce defaults to zero and stays there. It is Wormhole's field for
    batching messages, and this service sends one message per transfer — the
    *sequence*, not the nonce, is what makes a transfer unique, and the recorded
    transfer used zero too.
    """
    return Message.new_with_blockhash(
        [
            approve_instruction(
                source=accounts.from_token_account,
                delegate=accounts.authority_signer,
                owner=accounts.payer,
                amount=amount,
            ),
            transfer_native_instruction(
                accounts=accounts,
                token_bridge_program=token_bridge_program,
                nonce=nonce,
                amount=amount,
                recipient=recipient,
                target_chain=target_chain,
            ),
        ],
        accounts.payer,
        Hash.from_string(blockhash),
    )


def populate_transaction(message: Message, signatures: list[bytes]) -> bytes:
    """Assemble signed bytes from a message and the signatures over it.

    Split out from signing so the signing itself can go through
    `TransactionSigner` — which is what lets a custody service (phase 9) provide
    the payer's signature without this module knowing anything about it.
    """
    transaction = Transaction.populate(
        message, [Signature.from_bytes(signature) for signature in signatures]
    )
    return bytes(transaction)
