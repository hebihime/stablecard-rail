"""Wormhole behind `BridgeProvider` — the same two calls the simulator answers.

The funding engine cannot tell these apart, which was §9.2's bet and is this
phase's test of it. What differs is entirely inside:

**`submit` is idempotent by construction, because Wormhole has no idempotency
key.** A duplicate would lock a second amount and produce a second VAA, and the
engine retries `submit` precisely when it cannot tell whether the first call
landed. So the transfer's message account is derived from `order_ref` (§10.4):
before sending, the account is checked — if it exists, this order was already
submitted and its sequence is read back rather than re-sent. The window between
send and confirmation is closed the same way, because the check is a read of
on-chain state rather than a memory of what this process did.

**`status` acts.** It fetches the VAA, and if the guardians have signed and the
destination has not redeemed, it *submits the redemption*. A pure read would leave
every transfer permanently pending, because with lock-and-mint nobody else is
coming (§10.2, point 1). §9.2 chose two calls on the grounds that anything richer
"belongs inside an implementation until a caller needs it" — this is that, and the
engine and reconciler are unchanged.

**Three states, mapped honestly.** `PENDING` covers everything up to delivery:
sent-not-signed, signed-not-redeemed, redemption-in-flight. `COMPLETED` means the
destination Token Bridge says the transfer is delivered — not that we sent a
redemption, and not that a receipt looked good. `FAILED` is reserved for a refusal
the chain will repeat, and even then the money is recoverable, which is why the
failure reason is carried rather than swallowed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.chain.bridge.base import (
    BridgeError,
    BridgeOrder,
    BridgeProvider,
    BridgeRejectedError,
    BridgeStatus,
    BridgeTransfer,
    UnknownTransferError,
)
from app.chain.bridge.wormhole.accounts import (
    emitter_address,
    message_keypair_from_signature,
    message_seed_payload,
    parse_posted_message,
    transfer_accounts,
)
from app.chain.bridge.wormhole.client import WormholescanClient
from app.chain.bridge.wormhole.config import WormholeSettings
from app.chain.bridge.wormhole.instructions import build_transfer_message, populate_transaction
from app.chain.bridge.wormhole.redeemer import AlreadyDelivered, Redeemer, RedemptionRefused
from app.chain.bridge.wormhole.vaa import (
    MAX_WORMHOLE_DECIMALS,
    SignedVaa,
    parse_token_transfer,
    vaa_id,
)
from app.chain.rpc import SolanaRpcClient, SolanaRpcError
from app.chain.signer import TransactionSigner
from app.chain.tokens import associated_token_address
from app.core.money import Money
from app.core.time import utcnow

__all__ = ["BRIDGE_ID", "WormholeBridge"]

logger = logging.getLogger(__name__)

BRIDGE_ID = "wormhole"

#: How long to wait for a sent transaction's message account to become readable,
#: and how often to look. `sendTransaction` returns when the node *accepts* the
#: transaction, and the account is read at `finalized` — roughly 32 slots behind,
#: which is about thirteen seconds. Reading once and giving up made every healthy
#: first submit report a failure; found by the first live transfer, not by any
#: fixture, because a stubbed node answers instantly.
CONFIRM_ATTEMPTS = 10
CONFIRM_DELAY_SECONDS = 3.0

#: How a `bridge_ref` is read back apart. Ours to write and ours to parse; §9.2's
#: rule about opaque references binds every module *outside* this package.
_REF_PARTS = 3


Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Submitted:
    """A transfer this service has sent, identified before it was signed."""

    sequence: int
    signature: str | None


class WormholeBridge(BridgeProvider):
    """Solana devnet → BSC testnet, over Wormhole's Wrapped Token Transfers."""

    bridge_id = BRIDGE_ID

    def __init__(
        self,
        *,
        solana: SolanaRpcClient,
        guardians: WormholescanClient,
        redeemer: Redeemer,
        signer: TransactionSigner,
        settings: WormholeSettings,
        mint: str,
        decimals: int = 6,
        currency: str = "USD",
        confirm_attempts: int = CONFIRM_ATTEMPTS,
        confirm_delay_seconds: float = CONFIRM_DELAY_SECONDS,
        sleep: Sleeper | None = None,
    ) -> None:
        if decimals > MAX_WORMHOLE_DECIMALS:
            # Above eight, a VAA's amount is scaled and this adapter would have to
            # scale it back before calling it minor units. USDC has six; a token
            # that does not belongs to a phase that has thought about it.
            raise ValueError(
                f"this adapter assumes a token of at most {MAX_WORMHOLE_DECIMALS} decimals, "
                f"got {decimals}"
            )
        self._solana = solana
        self._guardians = guardians
        self._redeemer = redeemer
        self._signer = signer
        self._settings = settings
        self._mint = Pubkey.from_string(mint)
        self._decimals = decimals
        #: The chain knows a token; the ledger knows a currency. This is the one
        #: place the two are tied together, and it is configuration rather than
        #: inference because no VAA carries a currency code.
        self._currency = currency
        self._core = Pubkey.from_string(settings.core_program)
        self._token_bridge = Pubkey.from_string(settings.token_bridge_program)
        self._confirm_attempts = confirm_attempts
        self._confirm_delay = confirm_delay_seconds
        self._sleep: Sleeper = sleep or asyncio.sleep

    # ------------------------------------------------------------- submit ---

    async def submit(self, order: BridgeOrder) -> BridgeTransfer:
        if order.amount.amount_minor <= 0:
            raise BridgeRejectedError(
                f"a bridge order must move a positive amount, got {order.amount}"
            )
        recipient = _evm_recipient(order.destination_address)

        message = await self._message_keypair(order.order_ref)
        already = await self._already_submitted(message.pubkey())
        if already is not None:
            # The whole idempotency mechanism, in three lines: the chain already
            # holds this order's message, so this is a retry of a call that
            # landed. Re-sending would lock a second amount.
            logger.info(
                "order %s was already submitted as sequence %s; not sending again",
                order.order_ref,
                already.sequence,
            )
            return self._transfer(order, sequence=already.sequence, status=BridgeStatus.PENDING)

        owner = Pubkey.from_string(self._signer.public_key)
        accounts = transfer_accounts(
            payer=owner,
            from_token_account=associated_token_address(owner, self._mint),
            mint=self._mint,
            message=message.pubkey(),
            token_bridge_program=self._token_bridge,
            core_bridge_program=self._core,
        )
        blockhash = await self._solana.get_latest_blockhash()
        payload = build_transfer_message(
            accounts=accounts,
            token_bridge_program=self._token_bridge,
            amount=order.amount.amount_minor,
            recipient=recipient,
            target_chain=self._settings.destination_chain_id,
            blockhash=blockhash,
        )
        raw = bytes(payload)
        signed = populate_transaction(
            payload, [await self._signer.sign(raw), bytes(message.sign_message(raw))]
        )

        try:
            signature = await self._solana.send_transaction(signed)
        except SolanaRpcError:
            # The dangerous window: the send may have landed even though the
            # answer did not. Ask the chain rather than assume — assuming "it
            # failed" is exactly what produces a second locked amount.
            recovered = await self._already_submitted(message.pubkey())
            if recovered is None:
                raise
            logger.warning(
                "the send for order %s failed but the message exists; treating it as submitted",
                order.order_ref,
            )
            return self._transfer(order, sequence=recovered.sequence, status=BridgeStatus.PENDING)

        posted = await self._require_posted(message.pubkey(), order.order_ref)
        logger.info(
            "submitted order=%s as sequence=%s signature=%s",
            order.order_ref,
            posted.sequence,
            signature,
        )
        return self._transfer(
            order, sequence=posted.sequence, status=BridgeStatus.PENDING, signature=signature
        )

    # ------------------------------------------------------------- status ---

    async def status(self, bridge_ref: str) -> BridgeTransfer:
        """Where the transfer is — and, if it is waiting on us, a nudge.

        Not a pure read, deliberately. See the module docstring.
        """
        emitter_chain, emitter, sequence = _parse_ref(bridge_ref)

        vaa = await self._guardians.fetch_vaa(
            emitter_chain=emitter_chain, emitter_address=emitter, sequence=sequence
        )
        if vaa is None:
            # The guardians have not signed yet. Ordinary for a young transfer,
            # and nothing to do but ask again.
            return BridgeTransfer(
                bridge_id=self.bridge_id,
                bridge_ref=bridge_ref,
                order_ref="",
                status=BridgeStatus.PENDING,
                amount_in=Money(amount_minor=0, currency=self._currency),
                submitted_at=utcnow(),
                raw={"stage": "awaiting-guardians"},
            )

        transfer = parse_token_transfer(vaa.payload)
        amount = Money(amount_minor=transfer.amount, currency=self._currency)

        if await self._redeemer.is_delivered(vaa.digest):
            return self._from_vaa(
                bridge_ref, vaa, amount, status=BridgeStatus.COMPLETED, amount_out=amount
            )

        try:
            tx_hash = await self._redeemer.redeem(vaa.raw)
        except AlreadyDelivered:
            # Somebody else redeemed it between the check above and this call —
            # two workers, or a driver racing a reconciler. The money arrived, so
            # this is a completion, not a failure.
            logger.info("transfer %s was delivered by another attempt", bridge_ref)
            return self._from_vaa(
                bridge_ref, vaa, amount, status=BridgeStatus.COMPLETED, amount_out=amount
            )
        except RedemptionRefused as refused:
            # The chain will repeat this. The money is still recoverable with
            # this VAA, so the reason is carried into the ledger rather than
            # thrown away (§10.2, point 2).
            return self._from_vaa(
                bridge_ref,
                vaa,
                amount,
                status=BridgeStatus.FAILED,
                failure_reason=refused.reason,
            )

        # Submitted, not delivered. The next poll asks the chain, not this
        # receipt — a receipt is evidence about a transaction, and delivery is a
        # fact about the bridge.
        return self._from_vaa(
            bridge_ref,
            vaa,
            amount,
            status=BridgeStatus.PENDING,
            raw={"stage": "redeeming", "redemption_tx": tx_hash},
        )

    # ------------------------------------------------------------ internals ---

    async def _message_keypair(self, order_ref: str) -> Keypair:
        signature = await self._signer.sign(message_seed_payload(order_ref))
        return message_keypair_from_signature(signature)

    async def _already_submitted(self, message: Pubkey) -> _Submitted | None:
        account = await self._solana.get_account_info(str(message))
        if account is None:
            return None
        posted = parse_posted_message(_account_data(account))
        return _Submitted(sequence=posted.sequence, signature=None)

    async def _require_posted(self, message: Pubkey, order_ref: str) -> _Submitted:
        """Wait for the message this send created, then read its sequence.

        A send returns when the node accepts the transaction; the account is read
        at `finalized`, which trails by about thirteen seconds. So this polls
        rather than asking once — asking once meant every healthy submit reported
        a retryable failure, which the engine then recovered from on its next pass
        by finding the account. Correct, but it burned a retry and read like a bug
        in the logs, and it was: found by the first real transfer.

        Deliberately `finalized` rather than `confirmed`, even though `confirmed`
        would answer in about a second. The sequence read here *becomes* the
        `bridge_ref`, and a confirmed block can still be dropped — while the
        guardians will not sign before finality anyway, so waiting costs nothing
        that was not going to be waited for.
        """
        for attempt in range(1, self._confirm_attempts + 1):
            submitted = await self._already_submitted(message)
            if submitted is not None:
                return submitted
            if attempt < self._confirm_attempts:
                await self._sleep(self._confirm_delay)

        # Still nothing. Retryable, and the retry is safe for the same reason the
        # duplicate-submit path is: the next attempt reads this same account, so
        # it recovers the sequence instead of sending again.
        raise BridgeError(
            f"order {order_ref} was sent but its message account was not readable within "
            f"{self._confirm_attempts * self._confirm_delay:.0f}s; a retry will pick it up",
            retryable=True,
        )

    def _transfer(
        self,
        order: BridgeOrder,
        *,
        sequence: int,
        status: BridgeStatus,
        signature: str | None = None,
    ) -> BridgeTransfer:
        emitter = _emitter_hex(self._token_bridge)
        return BridgeTransfer(
            bridge_id=self.bridge_id,
            bridge_ref=vaa_id(self._settings.source_chain_id, emitter, sequence),
            order_ref=order.order_ref,
            status=status,
            amount_in=order.amount,
            submitted_at=utcnow(),
            raw={"sequence": sequence, "source_signature": signature},
        )

    def _from_vaa(
        self,
        bridge_ref: str,
        vaa: SignedVaa,
        amount: Money,
        *,
        status: BridgeStatus,
        amount_out: Money | None = None,
        failure_reason: str | None = None,
        raw: dict[str, object] | None = None,
    ) -> BridgeTransfer:
        return BridgeTransfer(
            bridge_id=self.bridge_id,
            bridge_ref=bridge_ref,
            order_ref="",
            status=status,
            amount_in=amount,
            amount_out=amount_out,
            failure_reason=failure_reason,
            submitted_at=vaa.observed_at,
            completed_at=vaa.observed_at if status is BridgeStatus.COMPLETED else None,
            raw=raw or {"digest": vaa.digest.hex()},
        )


def _emitter_hex(token_bridge_program: Pubkey) -> str:
    return bytes(emitter_address(token_bridge_program)).hex()


def _account_data(account: dict[str, object]) -> bytes:
    data = account.get("data")
    if not isinstance(data, list) or not data:
        raise BridgeError("the message account came back without data")
    return base64.b64decode(str(data[0]))


def _evm_recipient(address: str) -> bytes:
    """A 20-byte EVM address in Wormhole's 32-byte form, left-padded.

    Refused rather than coerced when it is the wrong shape: a right-padded
    address is a different, valid-looking recipient, and the funds would arrive
    somewhere nobody controls.
    """
    text = address.removeprefix("0x")
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise BridgeRejectedError(f"{address!r} is not an EVM address") from exc
    if len(raw) != 20:
        raise BridgeRejectedError(f"an EVM recipient is 20 bytes, got {len(raw)} from {address!r}")
    return b"\x00" * 12 + raw


def _parse_ref(bridge_ref: str) -> tuple[int, str, int]:
    parts = bridge_ref.split("/")
    if len(parts) != _REF_PARTS:
        raise UnknownTransferError(bridge_ref)
    chain, emitter, sequence = parts
    try:
        return int(chain), emitter, int(sequence)
    except ValueError as exc:
        raise UnknownTransferError(bridge_ref) from exc
