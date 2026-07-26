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

__all__ = ["GAS_LIMIT_HEADROOM", "Redeemer", "RedemptionRefused"]

logger = logging.getLogger(__name__)

#: Estimated gas is exact for the state at estimation time, and a redemption runs
#: a signature check whose cost moves with the guardian set. A fifth over is
#: cheap; running out is a failed transaction that still pays for the gas it burnt.
GAS_LIMIT_HEADROOM = 1.2


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
                # spending anything. This is where "already completed" and
                # "invalid emitter" surface.
                raise RedemptionRefused(exc.revert_reason or str(exc)) from exc
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

        tx_hash = await self._rpc.send_raw_transaction(signed)
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
