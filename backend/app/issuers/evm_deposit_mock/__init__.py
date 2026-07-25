"""The mock crypto-deposit issuer.

A package rather than one file because it ships the *provider* as well as the
adapter: `adapter.py` is the one file a real issuer needs, `simulator.py` and
`signing.py` stand in for servers that, for Lithic and Stripe, belong to someone
else.
"""

from __future__ import annotations

from app.issuers.evm_deposit_mock.adapter import EVENT_TYPE_MAP, EvmDepositMockAdapter
from app.issuers.evm_deposit_mock.simulator import (
    PROVIDER_ID,
    Delivery,
    MockIssuerSimulator,
    deposit_address_for,
)

__all__ = [
    "EVENT_TYPE_MAP",
    "PROVIDER_ID",
    "Delivery",
    "EvmDepositMockAdapter",
    "MockIssuerSimulator",
    "deposit_address_for",
]
