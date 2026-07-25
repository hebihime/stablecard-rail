"""Money is integer minor units, always. Floats must be impossible to smuggle in."""

from __future__ import annotations

import pytest

from app.core.money import Money


def test_amount_is_stored_as_given_minor_units() -> None:
    assert Money(2500, "USD").amount_minor == 2500


@pytest.mark.parametrize("bad", [25.0, 25.5, "2500", None, True])
def test_non_integer_amounts_are_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        Money(bad, "USD")  # type: ignore[arg-type]


def test_currency_is_normalised_to_upper_case() -> None:
    assert Money(1, "usd").currency == "USD"


@pytest.mark.parametrize("bad", ["US", "USDC", "U5D", ""])
def test_currency_must_be_three_letters(bad: str) -> None:
    with pytest.raises(ValueError):
        Money(1, bad)


def test_negative_amounts_are_allowed_for_reversals() -> None:
    assert Money(-500, "USD").amount_minor == -500


def test_is_immutable() -> None:
    money = Money(100, "USD")
    with pytest.raises(AttributeError):
        money.amount_minor = 200  # type: ignore[misc]


def test_equality_requires_matching_currency() -> None:
    assert Money(100, "USD") == Money(100, "usd")
    assert Money(100, "USD") != Money(100, "EUR")


def test_str_is_human_readable_without_float_maths() -> None:
    assert str(Money(2500, "USD")) == "2500 USD (minor units)"
