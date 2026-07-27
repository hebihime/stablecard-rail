"""Reading a Safe's balance off a real chain (SPEC.md §3.2, revised).

The one piece of this provider that is not a simulation. When
`GNOSIS_PAY_MOCK_SAFE_ADDRESS` is set, the Safe is an address on a testnet and this
is how the adapter finds out what it holds — an `eth_call` to `balanceOf`, nothing
more.

**Why this makes the funding model executable rather than modelled.** For a
`CRYPTO_DEPOSIT` provider there is no second pot of money to reconcile against: the
Safe's balance *is* the card's spending power. So reading it converts the central
claim of this project from an assertion into something checkable — against a public
RPC, by anyone, with no credentials. A fiat-rail issuer cannot offer that even on
mainnet, because its balance and the crypto that funded it are different money
(docs/ARCHITECTURE.md §13.2).

It lives in `issuers/gnosis_pay_mock/` because it is this provider's business, and
it imports `app.chain` — which adapters may do. Only `app.funding`, `app.ledger`,
`app.webhooks` and `app.api` are off limits, and `tests/test_module_boundaries.py`
holds that line.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.chain.evm.abi import selector
from app.chain.evm.config import get_evm_settings
from app.chain.evm.rpc import EvmRpcClient
from app.issuers.gnosis_pay_mock.config import GnosisPayMockSettings

__all__ = ["ObservedBalance", "SafeBalanceReader", "safe_reader_from_settings"]

#: `balanceOf(address)`. Computed rather than pasted, so it cannot be mistyped.
BALANCE_OF = selector("balanceOf(address)")


@dataclass(frozen=True, slots=True)
class ObservedBalance:
    """What the chain said, and what was asked to learn it."""

    units: int
    #: How this was learned, carried through to the deposit record.
    #:
    #: A balance read cannot name the transaction that produced the balance, so this
    #: names the read instead. Manufacturing a plausible transaction hash would make
    #: `issuer_funding_ref` a fiction, which is worse than it being unusual.
    evidence: str


class SafeBalanceReader:
    """One `eth_call`, and the arithmetic that turns a token amount into units.

    Token units, not minor units. The simulator counts in the token's own decimals
    (`SafeCurrency.decimals`) and so does this, which means the two agree without a
    conversion in between — and the conversion is exactly where a decimals mistake
    hides. `token_decimals` and the Safe currency's `decimals` must match, and
    `safe_reader_from_settings` refuses to build a reader when they do not.
    """

    def __init__(
        self,
        client: EvmRpcClient,
        *,
        token_address: str,
        token_decimals: int,
        chain: str,
    ) -> None:
        self._client = client
        self._token_address = token_address
        self._token_decimals = token_decimals
        self._chain = chain

    @property
    def token_decimals(self) -> int:
        return self._token_decimals

    async def balance_of(self, safe_address: str) -> ObservedBalance:
        """The Safe's ERC-20 balance, in the token's own units.

        Reads at `latest` rather than at a fixed confirmation depth. On BSC testnet a
        `latest` balance can in principle be reorganised away, and the honest note is
        that this demo accepts that: the alternative is holding a block number and
        waiting, which buys certainty this provider cannot use — the money either
        stays or the next read shows less, and `reconcile_safe` never retracts.
        """
        word = await self._client.call_contract(
            to=self._token_address,
            data=BALANCE_OF + _address_arg(safe_address),
        )
        return ObservedBalance(
            units=_decode_uint(word),
            evidence=(f"balanceOf({self._token_address}) for {safe_address} on {self._chain}"),
        )


def safe_reader_from_settings(settings: GnosisPayMockSettings) -> SafeBalanceReader | None:
    """A reader, or `None` when the Safe is an object rather than an address.

    `None` is the default and the common case: no address configured means no
    network, which is what keeps the offline demo offline and the suite silent.
    """
    if not settings.safe_is_onchain:
        return None
    evm = get_evm_settings()
    return SafeBalanceReader(
        EvmRpcClient(rpc_url=evm.rpc_url, timeout=evm.request_timeout_seconds),
        token_address=settings.safe_token_address,
        token_decimals=settings.safe_token_decimals,
        # From the chain settings, so the label and the node cannot disagree about
        # which chain is being read.
        chain=f"chain-{evm.chain_id}",
    )


def _address_arg(address: str) -> bytes:
    """An address, left-padded into one 32-byte word.

    `int(…, 16)` rather than string slicing: it accepts a `0x` prefix or not, and it
    rejects anything that is not hex instead of silently encoding it.
    """
    return int(address, 16).to_bytes(32, "big")


def _decode_uint(word: str) -> int:
    """One `uint256` out of an `eth_call` result.

    `0x` is a real answer and means zero — it is what a node returns for a call to an
    address holding no contract, which for a token address is a configuration
    mistake rather than an empty balance. Reported as zero anyway: the alternative is
    raising, and a zero balance and a wrong token address both correctly produce "no
    money here" for a provider that only ever adds what it can see.
    """
    if word in ("0x", ""):
        return 0
    return int(word, 16)
