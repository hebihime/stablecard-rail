"""Issuer adapters and the registry that finds them (SPEC.md §3).

**This file is the "registry entry" half of "a new issuer is one adapter file
plus one registry entry".** Adding a provider means writing the adapter and
adding one `register()` line below — nothing else in the service changes. That
claim is enforced structurally by `tests/test_module_boundaries.py`, which fails
if any module outside this package imports anything other than `base` and
`registry`.

Registration happens at import time and is by factory, so importing
`app.issuers.registry` anywhere makes every provider resolvable without any
module needing to know which adapters exist.
"""

from __future__ import annotations

from app.issuers.evm_deposit_mock import EvmDepositMockAdapter
from app.issuers.lithic import LithicAdapter
from app.issuers.registry import register

# --- the registry ----------------------------------------------------------
register(EvmDepositMockAdapter.provider_id, EvmDepositMockAdapter.from_settings)
# Registered whether or not LITHIC_API_KEY is set: the factory runs on first use, so
# an environment with no Lithic credentials still starts, and only a call that needs
# them fails (with a message naming the variable).
register(LithicAdapter.provider_id, LithicAdapter.from_settings)
# phase 4: register(StripeIssuingAdapter.provider_id, StripeIssuingAdapter.from_settings)
