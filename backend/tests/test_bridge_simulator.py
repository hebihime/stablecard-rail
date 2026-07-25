"""The bridge interface, and the simulator the demo runs on (SPEC.md §5.2).

The simulator exists so the end-to-end demo never depends on a third party's
testnet being up. That only holds if it is *deterministic*: no random failure
rate, no sleeping, no wall clock. Latency is a clock the caller supplies and
failures are a mode the caller selects, so every test here — and every demo run —
produces the same sequence of states.

The engine (phase 5e) must not be able to tell this apart from the real adapter
phase 6 adds, which is why the failure modes are the shapes a real bridge fails
in: unavailable, refused, accepted-then-failed, and accepted-then-silent.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from app.chain.bridge.base import (
    BridgeError,
    BridgeOrder,
    BridgeProvider,
    BridgeRejectedError,
    BridgeStatus,
    BridgeTransfer,
    UnknownTransferError,
)
from app.chain.bridge.simulated import (
    BridgeFailureMode,
    SimulatedBridge,
    SimulatedBridgeSettings,
)
from app.core.money import Money

START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class Clock:
    """A clock the test moves by hand. The alternative is `sleep`, which lies."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def an_order(
    order_ref: str = "intent-1",
    amount_minor: int = 25_000,
    currency: str = "USD",
) -> BridgeOrder:
    return BridgeOrder(
        order_ref=order_ref,
        amount=Money(amount_minor, currency),
        source_chain="solana-devnet",
        destination_chain="gnosis-chiado",
        destination_address="0xSafe000000000000000000000000000000000001",
    )


# -------------------------------------------------------------- interface ----


@pytest.mark.parametrize("name", ["submit", "status"])
def test_the_interface_is_two_async_methods(name: str) -> None:
    method = getattr(BridgeProvider, name)
    assert inspect.iscoroutinefunction(method)
    assert name in BridgeProvider.__abstractmethods__


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BridgeProvider()  # type: ignore[abstract]


def test_the_simulator_is_one_of_them() -> None:
    # The engine holds a `BridgeProvider`. Phase 6's deBridge adapter arrives as
    # a second implementation and nothing in `funding/` changes.
    assert isinstance(SimulatedBridge(), BridgeProvider)
    assert SimulatedBridge().bridge_id == "simulated"


def test_a_transfer_is_immutable_and_rejects_unknown_fields() -> None:
    transfer = BridgeTransfer(
        bridge_id="simulated",
        bridge_ref="sim_1",
        order_ref="intent-1",
        status=BridgeStatus.PENDING,
        amount_in=Money(100, "USD"),
        submitted_at=START,
    )
    with pytest.raises(ValueError):
        transfer.status = BridgeStatus.COMPLETED
    with pytest.raises(ValueError):
        BridgeTransfer(  # type: ignore[call-arg]
            bridge_id="simulated",
            bridge_ref="sim_1",
            order_ref="intent-1",
            status=BridgeStatus.PENDING,
            amount_in=Money(100, "USD"),
            submitted_at=START,
            debridge_specific_thing="nope",
        )


def test_a_transfer_timestamp_must_be_aware() -> None:
    with pytest.raises(ValueError, match="UTC"):
        BridgeTransfer(
            bridge_id="simulated",
            bridge_ref="sim_1",
            order_ref="intent-1",
            status=BridgeStatus.PENDING,
            amount_in=Money(100, "USD"),
            submitted_at=datetime(2026, 7, 26, 12, 0),  # naive on purpose
        )


# ----------------------------------------------------------------- submit ----


async def test_submit_accepts_an_order_and_reports_it_in_flight() -> None:
    bridge = SimulatedBridge(latency_seconds=30, clock=Clock())

    transfer = await bridge.submit(an_order())

    assert transfer.status is BridgeStatus.PENDING
    assert transfer.order_ref == "intent-1"
    assert transfer.bridge_ref  # opaque, but there is one to store on the intent
    assert transfer.amount_in == Money(25_000, "USD")
    # Nothing has arrived yet, so there is no amount and no destination tx to
    # name. `None` says "not observed", the way `issuer_funding_ref` does (§7.5).
    assert transfer.amount_out is None
    assert transfer.destination_tx_ref is None
    assert transfer.submitted_at == START


async def test_a_resubmitted_order_returns_the_same_transfer() -> None:
    # The engine retries `submit` whenever it cannot tell whether the first call
    # landed — a timeout, or a crash between the call and the state write. Two
    # bridge orders for one deposit would send the money twice.
    bridge = SimulatedBridge(latency_seconds=30, clock=Clock())

    first = await bridge.submit(an_order())
    second = await bridge.submit(an_order())

    assert first.bridge_ref == second.bridge_ref
    assert first.submitted_at == second.submitted_at


async def test_a_resubmitted_order_that_disagrees_about_the_amount_is_refused() -> None:
    # Stripe answers 409 to the same idempotency key with a different body, and
    # for the same reason: one of the two callers is wrong about what it asked
    # for, and guessing which would be guessing with money.
    bridge = SimulatedBridge(clock=Clock())
    await bridge.submit(an_order(amount_minor=25_000))

    with pytest.raises(BridgeRejectedError, match="differs"):
        await bridge.submit(an_order(amount_minor=30_000))


@pytest.mark.parametrize("amount_minor", [0, -1])
async def test_an_order_must_move_a_positive_amount(amount_minor: int) -> None:
    with pytest.raises(BridgeRejectedError, match="positive"):
        await SimulatedBridge().submit(an_order(amount_minor=amount_minor))


# ------------------------------------------------------------- completion ----


async def test_the_transfer_stays_pending_until_the_latency_elapses() -> None:
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=30, clock=clock)
    submitted = await bridge.submit(an_order())

    clock.advance(29)
    assert (await bridge.status(submitted.bridge_ref)).status is BridgeStatus.PENDING

    clock.advance(1)
    assert (await bridge.status(submitted.bridge_ref)).status is BridgeStatus.COMPLETED


async def test_completion_reports_what_arrived_and_where() -> None:
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=30, clock=clock)
    submitted = await bridge.submit(an_order())
    clock.advance(30)

    settled = await bridge.status(submitted.bridge_ref)

    assert settled.status is BridgeStatus.COMPLETED
    assert settled.amount_out == Money(25_000, "USD")
    assert settled.destination_tx_ref
    assert settled.completed_at == START + timedelta(seconds=30)
    assert settled.failure_reason is None


async def test_a_completed_transfer_does_not_move_when_asked_again() -> None:
    # The engine polls, the reconciler polls, and a drain may poll a third time.
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=1, clock=clock)
    submitted = await bridge.submit(an_order())
    clock.advance(1)

    first = await bridge.status(submitted.bridge_ref)
    clock.advance(3_600)
    second = await bridge.status(submitted.bridge_ref)

    assert first == second


async def test_the_bridge_fee_is_the_difference_between_in_and_out() -> None:
    # SPEC.md §11 calls out "bridged amounts net of fees" as a reconciliation
    # concern, so the simulator can charge one. The engine must fund the card
    # with what *arrived*, never with what was deposited.
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=1, fee_minor=150, clock=clock)
    submitted = await bridge.submit(an_order(amount_minor=25_000))
    clock.advance(1)

    settled = await bridge.status(submitted.bridge_ref)

    assert settled.amount_in == Money(25_000, "USD")
    assert settled.amount_out == Money(24_850, "USD")


async def test_a_fee_that_would_consume_the_transfer_is_refused_at_submit() -> None:
    # Better a refusal the engine can fail on than a completed transfer of zero.
    bridge = SimulatedBridge(fee_minor=25_000, clock=Clock())

    with pytest.raises(BridgeRejectedError, match="fee"):
        await bridge.submit(an_order(amount_minor=25_000))


# ------------------------------------------------------ failure injection ----


async def test_an_unavailable_bridge_is_worth_retrying() -> None:
    bridge = SimulatedBridge(failure_mode=BridgeFailureMode.SUBMIT_UNAVAILABLE)

    with pytest.raises(BridgeError) as caught:
        await bridge.submit(an_order())

    assert caught.value.retryable is True


async def test_a_refused_order_is_not_worth_retrying() -> None:
    bridge = SimulatedBridge(failure_mode=BridgeFailureMode.SUBMIT_REJECTED)

    with pytest.raises(BridgeRejectedError) as caught:
        await bridge.submit(an_order())

    assert caught.value.retryable is False


async def test_a_transfer_can_be_accepted_and_then_fail() -> None:
    # The expensive failure: the order was taken, so the engine is in BRIDGING
    # with a bridge_ref, and only a status poll reveals the money is not coming.
    clock = Clock()
    bridge = SimulatedBridge(
        latency_seconds=30, failure_mode=BridgeFailureMode.TRANSFER_FAILED, clock=clock
    )
    submitted = await bridge.submit(an_order())
    assert submitted.status is BridgeStatus.PENDING

    clock.advance(30)
    failed = await bridge.status(submitted.bridge_ref)

    assert failed.status is BridgeStatus.FAILED
    assert failed.failure_reason
    assert failed.amount_out is None


async def test_a_stuck_transfer_never_completes() -> None:
    # The reconciler's target (SPEC.md §5.3): a bridge that accepted the order
    # and then went quiet. Nothing but a staleness threshold can tell this from
    # a slow one, which is exactly why the threshold exists.
    clock = Clock()
    bridge = SimulatedBridge(latency_seconds=30, failure_mode=BridgeFailureMode.STUCK, clock=clock)
    submitted = await bridge.submit(an_order())

    clock.advance(86_400)

    assert (await bridge.status(submitted.bridge_ref)).status is BridgeStatus.PENDING


async def test_the_status_of_an_unknown_transfer_is_an_error_not_a_guess() -> None:
    with pytest.raises(UnknownTransferError):
        await SimulatedBridge().status("sim_nothing")


async def test_an_unknown_ref_is_unknown_even_when_other_transfers_exist() -> None:
    bridge = SimulatedBridge(clock=Clock())
    await bridge.submit(an_order(order_ref="intent-1"))

    with pytest.raises(UnknownTransferError):
        await bridge.status("sim_intent-2")


async def test_an_unknown_transfer_is_not_worth_retrying() -> None:
    # It will not become known by asking again. A retryable answer here would
    # spin the engine until the cap on a reference that does not exist.
    with pytest.raises(UnknownTransferError) as caught:
        await SimulatedBridge().status("sim_nothing")

    assert caught.value.retryable is False


# ---------------------------------------------------------- determinism -----


async def test_two_runs_of_the_same_order_agree_on_every_identifier() -> None:
    # The demo is recorded, and a recording that has to be re-shot because the
    # refs changed is a demo nobody re-runs. Derived from the order, not random.
    first = await SimulatedBridge(clock=Clock()).submit(an_order())
    second = await SimulatedBridge(clock=Clock()).submit(an_order())

    assert first == second


async def test_two_different_orders_do_not_collide() -> None:
    bridge = SimulatedBridge(clock=Clock())

    one = await bridge.submit(an_order(order_ref="intent-1"))
    two = await bridge.submit(an_order(order_ref="intent-2"))

    assert one.bridge_ref != two.bridge_ref


# ------------------------------------------------------------- settings -----


@pytest.mark.parametrize(
    ("kwargs", "complaint"),
    [
        ({"latency_seconds": -1}, "latency_seconds"),
        ({"fee_minor": -1}, "fee_minor"),
    ],
)
def test_the_simulator_refuses_a_configuration_that_cannot_mean_anything(
    kwargs: dict[str, float], complaint: str
) -> None:
    # Eagerly, in the constructor, by the rule §8.11 settled: a negative latency
    # would otherwise surface as a transfer that completed before it was
    # submitted, which sends whoever debugs it to the clock.
    with pytest.raises(ValueError, match=complaint):
        SimulatedBridge(**kwargs)  # type: ignore[arg-type]


def test_the_simulator_reads_its_own_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same rule as the adapters (§7.4): a component's configuration belongs to
    # the component, under a prefix nothing else answers to.
    monkeypatch.setenv("SIMULATED_BRIDGE_LATENCY_SECONDS", "12.5")
    monkeypatch.setenv("SIMULATED_BRIDGE_FEE_MINOR", "25")
    monkeypatch.setenv("SIMULATED_BRIDGE_FAILURE_MODE", "stuck")

    settings = SimulatedBridgeSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.latency_seconds == 12.5
    assert settings.fee_minor == 25
    assert settings.failure_mode is BridgeFailureMode.STUCK


def test_the_simulator_defaults_to_working() -> None:
    # Failure injection is something a demo asks for, never something it gets by
    # accident.
    settings = SimulatedBridgeSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.failure_mode is BridgeFailureMode.NONE
    assert settings.fee_minor == 0


async def test_a_bridge_built_from_settings_behaves_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIMULATED_BRIDGE_FAILURE_MODE", "submit_unavailable")
    clock = Clock()

    bridge = SimulatedBridge.from_settings(
        SimulatedBridgeSettings(_env_file=None),  # type: ignore[call-arg]
        clock=clock,
    )

    with pytest.raises(BridgeError):
        await bridge.submit(an_order())
