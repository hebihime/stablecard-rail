"""Money as integer minor units.

Every amount in this system — deposits, bridge outputs, card funding, ledger
entries — is an integer count of the currency's minor unit (cents for USD) plus
an ISO-4217 code. Floats are rejected at construction, not merely discouraged:
binary floating point cannot represent most decimal amounts exactly, and a
rounding error in a funding pipeline is a reconciliation incident.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Money"]


@dataclass(frozen=True, slots=True)
class Money:
    """An exact monetary amount. Negative values are legal (reversals, refunds)."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        # bool is an int subclass; `Money(True, "USD")` is a bug, not one cent.
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            kind = type(self.amount_minor).__name__
            raise TypeError(f"amount_minor must be an int of minor units, got {kind}")
        if not (len(self.currency) == 3 and self.currency.isalpha()):
            raise ValueError(f"currency must be a 3-letter ISO-4217 code, got {self.currency!r}")
        object.__setattr__(self, "currency", self.currency.upper())

    def __str__(self) -> str:
        return f"{self.amount_minor} {self.currency} (minor units)"
