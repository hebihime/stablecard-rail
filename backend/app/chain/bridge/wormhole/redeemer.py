"""Delivering a transfer — the leg a lock-and-mint bridge leaves to us.

With a solver-filled route, `BRIDGING → BRIDGED` happens whether or not this
service is running: somebody is paid to complete it. Lock-and-mint has no such
somebody. The guardians sign, and then the VAA sits there until a transaction
submits it to the destination chain (docs/ARCHITECTURE.md §10.2, point 1).

**`is_delivered` is asked first, always.** It is the authoritative answer to "did
the money arrive", it costs nothing, and it makes the whole operation restartable:
a redemption that was submitted and then lost — a crash, a timeout, a redeploy —
is discovered by asking rather than by re-sending. It is also what keeps a retry
from paying gas to fail on a transfer somebody else already delivered.

**The VAA is the only key to locked money.** Once the source transaction lands,
the USDC is in the Token Bridge's custody account and the signed VAA is the only
way out. It does not expire. So a redemption that fails is worth trying again
indefinitely, and the failure that must *not* be treated as terminal is a
transport failure — §10.2, point 2, and the reason `BridgeError.retryable` is set
so carefully here.
"""

from __future__ import annotations

import logging

from eth_utils.crypto import keccak

from app.chain.bridge.base import BridgeError
from app.chain.evm.abi import (
    COMPLETE_TRANSFER,
    IS_TRANSFER_COMPLETED,
    WRAPPED_ASSET,
    ZERO_ADDRESS,
    decode_address,
    decode_bool,
    encode_bytes32_arg,
    encode_bytes_arg,
    encode_uint16_and_bytes32,
    selector,
)
from app.chain.evm.rpc import EvmRpcClient, EvmRpcError
from app.chain.evm.signer import EvmTransaction, EvmTransactionSigner

__all__ = [
    "GAS_LIMIT_HEADROOM",
    "AlreadyDelivered",
    "OutOfGasMoney",
    "Redeemer",
    "RedemptionRefused",
]

logger = logging.getLogger(__name__)

#: Estimated gas is exact for the state at estimation time, and a redemption runs
#: a signature check whose cost moves with the guardian set. A fifth over is
#: cheap; running out is a failed transaction that still pays for the gas it burnt.
GAS_LIMIT_HEADROOM = 1.2


#: What a node says when the sender cannot pay for the gas it asked for. Not a
#: standard code — `-32000` covers half a dozen unrelated conditions — so the text
#: is what distinguishes it. Found by running out on BSC testnet, which is also
#: why the message forms below are the ones observed rather than the ones guessed.
OUT_OF_MONEY_PHRASES = ("insufficient funds", "insufficient balance")

#: And what it says when the transaction is already in its pool. Both mean the
#: send succeeded, once from this process and once from a previous attempt.
ALREADY_SUBMITTED_PHRASES = ("already known", "nonce too low", "already exists")


#: What the deployed Token Bridge reverts with when a VAA has already been
#: redeemed. **Observed, not guessed**: BSC testnet runs a version that uses
#: `require(..., "transfer already completed")`, where newer Wormhole releases use a
#: custom error (`TransferAlreadyCompleted()`) that carries no text at all. A
#: deployment using the custom error would fall through to `RedemptionRefused`,
#: which is why `is_delivered` remains the authoritative check rather than this.
ALREADY_COMPLETED_PHRASES = ("transfer already completed", "already completed")


class AlreadyDelivered(BridgeError):
    """The transfer was redeemed by somebody else between the check and the send.

    Good news wearing a revert's clothes, and worth its own class because the
    alternative is the worst misclassification available: a plain
    `RedemptionRefused` here would mark an intent `FAILED_BRIDGE` on money that
    *has arrived*. Two workers, or a driver racing a reconciler, is all it takes —
    and the repository already warns that two processes can run at once.

    Found by deliberately attempting a duplicate redemption against BSC testnet
    once a real transfer had been delivered (docs/ARCHITECTURE.md §10.7).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"the transfer was already delivered: {reason}", retryable=False)
        self.reason = reason


class OutOfGasMoney(BridgeError):
    """The redeemer's own address cannot pay for the redemption.

    **Retryable, and that is the whole point of the class.** It is an operational
    condition with an operational remedy — top the address up — and the money it
    would deliver is already locked on the source chain. Classified as permanent
    (which `-32000` otherwise is, and was) it would mark an intent `FAILED_BRIDGE`
    while the funds sat recoverable behind a VAA that never expires: §10.2's point
    2, arriving through a door I had not thought of until a live run walked
    through it.
    """

    def __init__(self, address: str, detail: str) -> None:
        super().__init__(
            f"the redeemer {address} cannot pay for this redemption ({detail}); "
            f"top it up and it will be retried",
            retryable=True,
        )
        self.address = address


class RedemptionRefused(BridgeError):
    """The destination chain will not accept this VAA, and will not later.

    A revert. Distinct from a transport failure, because the money is already
    locked on the source chain and giving up on a *transient* failure would strand
    it (§10.2, point 2). Never retryable — but see the caller: even this does not
    make a transfer terminal until somebody has looked at the reason.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"the destination chain refused the transfer: {reason}", retryable=False)
        self.reason = reason


class Redeemer:
    """Reads and writes the destination Token Bridge."""

    def __init__(
        self,
        rpc: EvmRpcClient,
        *,
        signer: EvmTransactionSigner,
        token_bridge: str,
        chain_id: int,
    ) -> None:
        self._rpc = rpc
        self._signer = signer
        self._token_bridge = token_bridge
        self._chain_id = chain_id

    @property
    def address(self) -> str:
        """Who pays the gas. Never who receives the funds."""
        return self._signer.address

    async def is_delivered(self, digest: bytes) -> bool:
        """Has this VAA already been redeemed here?

        The digest is the double-keccak from `vaa.py`. Getting that wrong makes
        every delivered transfer read as undelivered, which is why it has two
        independent confirmations behind it.
        """
        word = await self._rpc.call_contract(
            to=self._token_bridge,
            data=selector(IS_TRANSFER_COMPLETED) + encode_bytes32_arg(digest),
        )
        return decode_bool(word)

    async def wrapped_asset(self, *, token_chain: int, token_address: bytes) -> str | None:
        """The local token standing for a foreign one, or `None` if unattested.

        `None` rather than the zero address, because a zero here is not an
        address: it means nobody has attested the token, and a transfer of it
        would arrive as a claim on a contract that does not exist.
        """
        word = await self._rpc.call_contract(
            to=self._token_bridge,
            data=selector(WRAPPED_ASSET) + encode_uint16_and_bytes32(token_chain, token_address),
        )
        address = decode_address(word)
        return None if address == ZERO_ADDRESS else address

    async def redeem(self, vaa: bytes) -> str:
        """Submit the VAA, and return the transaction hash.

        Raises:
            RedemptionRefused: the chain reverted. Not retryable *as stated* —
                the caller decides whether that is terminal, because the money is
                locked and the VAA never expires.
            BridgeError: the node could not be reached, or refused the send for a
                reason that may pass. Retryable.
        """
        data = selector(COMPLETE_TRANSFER) + encode_bytes_arg(vaa)
        sender = self._signer.address

        try:
            estimated = await self._rpc.estimate_gas(
                to=self._token_bridge, data=data, sender=sender
            )
        except EvmRpcError as exc:
            if exc.reverted:
                # Free pre-flight: the chain says why, and says it without
                # spending anything. Two of the answers here mean opposite things,
                # so they are separated before either is called a failure.
                reason = exc.revert_reason or str(exc)
                if any(phrase in reason.lower() for phrase in ALREADY_COMPLETED_PHRASES):
                    raise AlreadyDelivered(reason) from exc
                raise RedemptionRefused(reason) from exc
            raise

        gas_price = await self._rpc.gas_price()
        nonce = await self._rpc.transaction_count(sender)
        signed = await self._signer.sign_transaction(
            EvmTransaction(
                to=self._token_bridge,
                data=data,
                nonce=nonce,
                gas=int(estimated * GAS_LIMIT_HEADROOM),
                gas_price=gas_price,
                chain_id=self._chain_id,
            )
        )

        # The hash is a property of the signed bytes, so it is knowable before the
        # node answers — which is what lets an "already known" reply be read as the
        # success it is rather than as a failure.
        expected_hash = "0x" + keccak(signed).hex()
        try:
            tx_hash = await self._rpc.send_raw_transaction(signed)
        except EvmRpcError as exc:
            lowered = exc.message.lower()
            if any(phrase in lowered for phrase in OUT_OF_MONEY_PHRASES):
                raise OutOfGasMoney(sender, exc.message) from exc
            if any(phrase in lowered for phrase in ALREADY_SUBMITTED_PHRASES):
                # A previous attempt's transaction is in the pool or already mined.
                # Same nonce, same bytes, same hash: nothing new to send.
                logger.info("the redemption was already submitted as %s", expected_hash)
                return expected_hash
            raise

        logger.info("submitted a redemption as %s, paying gas from %s", tx_hash, sender)
        return tx_hash

    async def receipt_status(self, tx_hash: str) -> bool | None:
        """`True` mined and succeeded, `False` mined and reverted, `None` not yet.

        Three answers because there are three, and collapsing "not yet" into
        either of the others is how a healthy redemption gets abandoned or a
        failed one gets believed.
        """
        receipt = await self._rpc.transaction_receipt(tx_hash)
        if receipt is None:
            return None
        return str(receipt.get("status")) == "0x1"
