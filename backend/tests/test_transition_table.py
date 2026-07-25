"""The legal-transition table itself (SPEC.md §5.1), asserted as a golden matrix.

This test exists so widening the state machine is never accidental: adding an
edge in `app/funding/states.py` fails here until it is added deliberately below.
"""

from __future__ import annotations

import itertools

import pytest

from app.funding.states import (
    RETRYABLE_STATES,
    TRANSITIONS,
    FundingState,
    is_legal_transition,
    is_terminal,
    legal_targets,
)

S = FundingState

# The single source of truth for what this machine is allowed to do.
# A state mapping to an empty set is terminal.
EXPECTED: dict[FundingState, set[FundingState]] = {
    S.PENDING: {S.PENDING, S.DEPOSIT_CONFIRMED, S.FAILED_DEPOSIT},
    S.DEPOSIT_CONFIRMED: {S.BRIDGING, S.FAILED_BRIDGE},
    S.BRIDGING: {S.BRIDGING, S.BRIDGED, S.FAILED_BRIDGE},
    S.BRIDGED: {S.FUNDING, S.FAILED_FUNDING},
    S.FUNDING: {S.FUNDING, S.FUNDED, S.FAILED_FUNDING},
    S.FUNDED: {S.SETTLED, S.FAILED_SETTLEMENT},
    S.SETTLED: set(),
    S.FAILED_DEPOSIT: set(),
    S.FAILED_BRIDGE: set(),
    S.FAILED_FUNDING: set(),
    S.FAILED_SETTLEMENT: set(),
}

HAPPY_PATH = [
    S.PENDING,
    S.DEPOSIT_CONFIRMED,
    S.BRIDGING,
    S.BRIDGED,
    S.FUNDING,
    S.FUNDED,
    S.SETTLED,
]


def test_table_covers_every_state_exactly_once() -> None:
    assert set(TRANSITIONS) == set(FundingState)
    assert set(EXPECTED) == set(FundingState)


def test_table_matches_the_golden_matrix() -> None:
    assert {state: set(targets) for state, targets in TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize("state", list(FundingState))
def test_legal_targets_agrees_with_the_table(state: FundingState) -> None:
    assert set(legal_targets(state)) == EXPECTED[state]


@pytest.mark.parametrize(
    ("frm", "to"), [(a, b) for a in FundingState for b in FundingState], ids=lambda s: str(s)
)
def test_is_legal_transition_agrees_with_the_table(frm: FundingState, to: FundingState) -> None:
    assert is_legal_transition(frm, to) is (to in EXPECTED[frm])


def test_happy_path_is_walkable_end_to_end() -> None:
    for frm, to in itertools.pairwise(HAPPY_PATH):
        assert is_legal_transition(frm, to), f"{frm} -> {to} must be legal"


def test_terminal_states_are_exactly_settled_and_failures() -> None:
    terminal = {state for state in FundingState if is_terminal(state)}
    assert terminal == {
        S.SETTLED,
        S.FAILED_DEPOSIT,
        S.FAILED_BRIDGE,
        S.FAILED_FUNDING,
        S.FAILED_SETTLEMENT,
    }


def test_every_failure_state_is_reachable() -> None:
    reachable = {to for targets in TRANSITIONS.values() for to in targets}
    for state in FundingState:
        if state.name.startswith("FAILED_"):
            assert state in reachable, f"{state} is unreachable — dead branch"


def test_retryable_states_are_exactly_the_self_looping_ones() -> None:
    # A retry is modelled as a self-transition (SPEC.md §5.3: retry with backoff
    # up to a cap, *then* mark FAILED_*), so the two definitions must agree.
    assert RETRYABLE_STATES == frozenset(
        state for state, targets in TRANSITIONS.items() if state in targets
    )
    assert RETRYABLE_STATES == {S.PENDING, S.BRIDGING, S.FUNDING}


def test_no_state_escapes_a_terminal_state() -> None:
    for state in FundingState:
        if is_terminal(state):
            assert not TRANSITIONS[state]


def test_states_are_plain_strings_for_storage() -> None:
    assert FundingState.PENDING == "PENDING"
    assert str(FundingState.FAILED_BRIDGE) == "FAILED_BRIDGE"
