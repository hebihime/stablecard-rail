"""The Gnosis Pay mock issuer.

A package rather than one file because it ships the *provider* as well as the
adapter: `adapter.py` is the one file a real issuer needs, `simulator.py` and
`signing.py` stand in for servers — and for a chain — that, for Lithic and Stripe,
belong to someone else.
"""

from __future__ import annotations

from app.issuers.gnosis_pay_mock.adapter import EVENT_TYPE_MAP, GnosisPayMockAdapter
from app.issuers.gnosis_pay_mock.simulator import (
    PROVIDER_ID,
    SAFE_CURRENCIES,
    Delivery,
    GnosisPaySimulator,
    SafeDeposit,
    safe_address_for,
)

__all__ = [
    "EVENT_TYPE_MAP",
    "PROVIDER_ID",
    "SAFE_CURRENCIES",
    "Delivery",
    "GnosisPayMockAdapter",
    "GnosisPaySimulator",
    "SafeDeposit",
    "safe_address_for",
]
