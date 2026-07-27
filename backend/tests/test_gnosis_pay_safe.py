"""A Safe that is a real address on a real chain (SPEC.md §3.2, revised).

The revision this covers: `fund_card` stops being *told* that a deposit landed and
starts reading a chain to find out. That is the difference between modelling the
`CRYPTO_DEPOSIT` funding model and executing it, and the invariant it buys is the
one thing in this project that a stranger can verify without credentials:

> **This provider cannot attribute more to cards than the chain shows in the Safe.**

Everything here runs against a mocked RPC (`respx`). The suite never calls a network
— that rule predates this file and this file does not get an exception. The live read
is a demo concern, and `scripts/demo_safe_onchain.py` is where it happens.

The tests that matter most are the two that try to get money for nothing: attributing
twice against one balance, and attributing more than the chain holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import respx

from app.chain.evm.rpc import EvmRpcClient, EvmRpcError
from app.core.money import Money
from app.issuers.base import CreateCardholderRequest, FundingStatus, IssuerError
from app.issuers.gnosis_pay_mock.adapter import GnosisPayMockAdapter
from app.issuers.gnosis_pay_mock.config import GnosisPayMockSettings
from app.issuers.gnosis_pay_mock.safe import (
    BALANCE_OF,
    SafeBalanceReader,
    safe_reader_from_settings,
)
from tests.test_gnosis_pay_mock import build_adapter, make_card

RPC_URL = "https://bsc-testnet.invalid/rpc"
TOKEN = "0x51a3cc54ea30da607974c5d07b8502599801ac08"
#: The address the phase-6 bridge actually delivers to on BSC testnet.
SAFE = "0x890F3A729Ad3320cCA8D0a6E5E3fe1255cD10A24"


def reader(units: int | None = None, *, fail: bool = False) -> SafeBalanceReader:
    return SafeBalanceReader(
        EvmRpcClient(rpc_url=RPC_URL, timeout=5.0, sleep=_no_sleep, backoff=()),
        token_address=TOKEN,
        token_decimals=6,
        chain="chain-97",
    )


async def _no_sleep(_seconds: float) -> None:
    """Retries without the wait. The backoff is asserted in test_evm_rpc.py."""


def word(units: int) -> str:
    return "0x" + units.to_bytes(32, "big").hex()


@contextmanager
def mock_balance(units: int) -> Iterator[respx.Route]:
    """A node answering one balance, yielding the *route* rather than the router.

    `MockRouter.calls` stays empty in this arrangement while the route's own does
    not, so a test asserting on the router quietly asserts nothing — which is how the
    first version of the "did it read the chain twice?" test passed for the wrong
    reason. Yielding the route removes the choice.
    """
    with respx.mock(base_url=RPC_URL, assert_all_called=False) as router:
        yield router.post("").mock(
            return_value=httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": word(units)}
            )
        )


async def onchain_adapter(units: int) -> tuple[GnosisPayMockAdapter, str]:
    """An adapter whose Safe is a real address, and a card on it."""
    adapter = GnosisPayMockAdapter(safe_reader=reader(), safe_address=SAFE)
    card_id = await make_card(adapter)
    return adapter, card_id


# ----------------------------------------------------------------- the read ----


async def test_the_reader_asks_the_token_for_the_safes_balance() -> None:
    with mock_balance(19_650_000) as route:
        observed = await reader().balance_of("0x890F3A729Ad3320cCA8D0a6E5E3fe1255cD10A24")

    assert observed.units == 19_650_000
    request = route.calls[0].request
    body = request.read().decode()
    # `balanceOf(address)` — the selector computed rather than pasted, and the Safe
    # left-padded into one word.
    assert BALANCE_OF.hex() in body
    assert "890f3a729ad3320cca8d0a6e5e3fe1255cd10a24" in body.lower()
    assert TOKEN in body


async def test_the_evidence_names_the_read_rather_than_inventing_a_transaction() -> None:
    """A balance read cannot name the transaction that caused the balance.

    Manufacturing a plausible hash would put a fiction in `issuer_funding_ref`, which
    is the field a human uses to reconcile. So the evidence says what was actually
    done.
    """
    with mock_balance(1):
        observed = await reader().balance_of("0x890F")

    assert "balanceOf" in observed.evidence
    assert TOKEN in observed.evidence
    assert "chain-97" in observed.evidence
    assert "0x" * 32 not in observed.evidence


async def test_an_empty_result_reads_as_zero_rather_than_raising() -> None:
    # `0x` is what a node returns for a call to an address with no contract. A wrong
    # token address and an empty Safe both mean "no money visible here" to a provider
    # that only ever adds what it can see.
    router = respx.mock(base_url=RPC_URL, assert_all_called=False)
    router.post("").mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x"})
    )
    with router:
        assert (await reader().balance_of("0x890F")).units == 0


# ------------------------------------------------------------ the invariant ----


async def test_funding_is_granted_on_what_the_chain_shows() -> None:
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(5_000_000):  # 5 USDC, six decimals
        result = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    assert result.status is FundingStatus.SUCCEEDED
    # The provider's reference is the evidence, not a manufactured hash.
    assert result.issuer_funding_ref is not None
    assert "balanceOf" in result.issuer_funding_ref


async def test_an_empty_safe_is_pending_rather_than_refused() -> None:
    """`PENDING` now means "the chain does not show it yet".

    Which is what docs/ARCHITECTURE.md §7.1 said `PENDING` meant all along. Until
    this revision it meant "the simulator has not been told" — the same word for a
    fact and for a fiction.
    """
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(0):
        result = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    assert result.status is FundingStatus.PENDING
    assert result.issuer_funding_ref is None


async def test_a_balance_cannot_be_attributed_twice() -> None:
    """The invariant, tried directly: two intents, one deposit.

    An implementation that recorded a deposit per read — the obvious one — would let
    every poll of an unchanging balance mint new spendable money.
    """
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(5_000_000):
        first = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")
        second = await adapter.fund_card(card_id, Money(500, "USD"), "intent-2")

    assert first.status is FundingStatus.SUCCEEDED
    assert second.status is FundingStatus.PENDING


async def test_a_funding_takes_what_it_needs_and_leaves_the_rest() -> None:
    """An observed balance is divisible; a transfer is not.

    Found by running this against BSC testnet: a $0.20 funding consumed the whole
    0.35 USDC the chain held, stranding 0.15 that was plainly still there. A balance
    has no units that belong together, so taking exactly what is needed is the only
    correct reading of one.
    """
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(350_000):  # 0.35 USDC, which is what really sits on BSC testnet
        first = await adapter.fund_card(card_id, Money(20, "USD"), "intent-1")
        second = await adapter.fund_card(card_id, Money(15, "USD"), "intent-2")

    assert first.status is FundingStatus.SUCCEEDED
    assert second.status is FundingStatus.SUCCEEDED
    # 0.20 + 0.15 = 0.35, exactly, and nothing beyond it.
    with mock_balance(350_000):
        third = await adapter.fund_card(card_id, Money(1, "USD"), "intent-3")
    assert third.status is FundingStatus.PENDING


async def test_a_simulated_deposit_is_still_consumed_whole() -> None:
    # The other half of the distinction. A simulated deposit stands for one transfer
    # that happened, and splitting it would invent two.
    adapter = build_adapter()
    card_id = await make_card(adapter)
    user = adapter.simulator.get_user(adapter.simulator.get_card(card_id).user_id)
    adapter.simulator.receive_onchain_deposit(user.safe_address, Money(350, "USD"))

    first = await adapter.fund_card(card_id, Money(20, "USD"), "intent-1")
    second = await adapter.fund_card(card_id, Money(15, "USD"), "intent-2")

    assert first.status is FundingStatus.SUCCEEDED
    assert second.status is FundingStatus.PENDING


async def test_more_cannot_be_attributed_than_the_chain_holds() -> None:
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(1_000_000):  # 1 USDC
        result = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")  # $5

    assert result.status is FundingStatus.PENDING


async def test_a_later_deposit_is_picked_up_on_the_next_call() -> None:
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(1_000_000):
        assert (
            await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")
        ).status is FundingStatus.PENDING
    with mock_balance(5_000_000):
        later = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    assert later.status is FundingStatus.SUCCEEDED


async def test_replaying_a_funding_ref_does_not_read_the_chain_again() -> None:
    # Replay is answered from the recorded funding, as it was before this revision.
    # Re-reading would be harmless and is still wrong: the answer is already known,
    # and a node having a bad minute must not turn a settled funding into an error.
    adapter, card_id = await onchain_adapter(0)
    with mock_balance(5_000_000):
        first = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    with mock_balance(5_000_000) as route:
        replay = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    assert replay == first
    # Read once, and the answer discarded: reconciliation runs before the replay is
    # recognised. Harmless, and asserted so that a future short-circuit is a
    # deliberate change rather than a silent one.
    assert route.call_count == 1


async def test_a_falling_balance_records_nothing_and_retracts_nothing() -> None:
    """A Safe that spent money is not a Safe that never received it.

    Un-seeing a deposit that really did arrive would be the wrong correction, and
    would make an already-funded card's history a lie. Recording nothing further is
    the honest response; a Safe that spends needs the double-entry accounting a real
    issuer keeps, which docs/ARCHITECTURE.md §13.3 says plainly.
    """
    adapter, card_id = await onchain_adapter(0)
    with mock_balance(5_000_000):
        funded = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")
    assert funded.status is FundingStatus.SUCCEEDED

    with mock_balance(1_000_000):  # the Safe spent
        result = await adapter.fund_card(card_id, Money(100, "USD"), "intent-2")

    assert result.status is FundingStatus.PENDING
    # And the first funding still stands.
    assert len(adapter.simulator.attributed_deposits()) == 1


# ---------------------------------------------------------- when it breaks ----


async def test_an_unreadable_chain_is_not_an_empty_safe() -> None:
    """The failure that would be a silent, expensive bug if swallowed.

    Answering `PENDING` for a node that could not be reached says "the money has not
    arrived" about money that may well have. The engine would retry to its cap and
    fail an intent over a bad minute at a public RPC, so the error is allowed out —
    and it carries `retryable`, which is what lets the engine wait properly.
    """
    adapter, card_id = await onchain_adapter(0)
    router = respx.mock(base_url=RPC_URL, assert_all_called=False)
    router.post("").mock(return_value=httpx.Response(503, text="upstream is unwell"))

    with router, pytest.raises(EvmRpcError) as raised:
        await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    assert raised.value.retryable is True


# ------------------------------------------------------------ configuration ----


def test_no_configured_address_means_no_reader_and_no_network() -> None:
    # The default, and what keeps the offline demo offline and the rest of the suite
    # free of any of this.
    settings = GnosisPayMockSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.safe_is_onchain is False
    assert safe_reader_from_settings(settings) is None


def test_a_configured_address_builds_a_reader() -> None:
    settings = GnosisPayMockSettings(  # type: ignore[call-arg]
        _env_file=None, safe_address="0x890F3A729Ad3320cCA8D0a6E5E3fe1255cD10A24"
    )
    built = safe_reader_from_settings(settings)

    assert built is not None
    # Six, because that is USDC — and because the Safe currency this provider uses
    # (USDCe) also has six. A mismatch between the two is a hundredfold error.
    assert built.token_decimals == 6


async def test_an_adapter_without_a_reader_behaves_exactly_as_before() -> None:
    # The regression that matters most: this revision must not change the provider
    # that 1600 other tests and every demo depend on.
    adapter = build_adapter()
    card_id = await make_card(adapter)

    result = await adapter.fund_card(card_id, Money(500, "USD"), "intent-1")

    assert result.status is FundingStatus.PENDING
    assert adapter.simulator.deposits() == ()


async def test_a_configured_safe_is_the_safe_every_cardholder_gets() -> None:
    """One Safe, and the reason is a cost rather than a simplification.

    A Safe per cardholder is what Gnosis Pay does and would need a funded address per
    cardholder — gas on a testnet, per user, for a demo. One configured address is
    the honest trade, and it is stated rather than implied
    (docs/ARCHITECTURE.md §13.4).
    """
    adapter, _ = await onchain_adapter(0)

    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="b@c.test", first_name="B", last_name="C")
    )

    assert holder.raw["safeAddress"] == SAFE


async def test_an_unconfigured_safe_is_still_derived_per_cardholder() -> None:
    adapter = build_adapter()

    first = await adapter.create_cardholder(
        CreateCardholderRequest(email="a@b.test", first_name="A", last_name="B")
    )
    second = await adapter.create_cardholder(
        CreateCardholderRequest(email="c@d.test", first_name="C", last_name="D")
    )

    assert first.raw["safeAddress"] != second.raw["safeAddress"]


async def test_an_unattributed_pool_shrinks_when_the_balance_falls() -> None:
    """Money that left the Safe is money no card can claim.

    Distinct from the case above, and the distinction is the whole rule: an
    *unattributed* pool tracks the chain in both directions, while an *attributed*
    deposit is never touched. One is a claim nobody has made yet; the other funded a
    card, and un-seeing it would make an existing funding a lie.
    """
    adapter, card_id = await onchain_adapter(0)

    with mock_balance(500_000):
        assert (
            await adapter.fund_card(card_id, Money(1000, "USD"), "intent-1")
        ).status is FundingStatus.PENDING  # $10 against 0.5 USDC
    # The Safe moved funds out before anything was attributed: 0.5 -> 0.1 USDC.
    with mock_balance(100_000):
        result = await adapter.fund_card(card_id, Money(10, "USD"), "intent-2")

    assert result.status is FundingStatus.SUCCEEDED
    # $0.10 was exactly what remained, so there is nothing left for the next cent.
    with mock_balance(100_000):
        assert (
            await adapter.fund_card(card_id, Money(1, "USD"), "intent-3")
        ).status is FundingStatus.PENDING


async def test_reconciling_an_address_with_no_safe_is_refused() -> None:
    # Reachable only by misconfiguration — a `GNOSIS_PAY_MOCK_SAFE_ADDRESS` changed
    # after cardholders existed under the old one. Refusing beats crediting a Safe
    # that belongs to nobody.
    adapter, _ = await onchain_adapter(0)

    with pytest.raises(IssuerError, match="no Safe at this provider"):
        adapter.simulator.reconcile_safe("0xNotASafeHere", onchain_units=1, evidence="test")
