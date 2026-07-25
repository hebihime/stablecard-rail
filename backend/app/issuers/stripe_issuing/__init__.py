"""The Stripe Issuing adapter (SPEC.md §3.2, §12.4) — the second real `FIAT_RAIL`
issuer, and the phase whose job is to test the abstraction rather than extend it.

`adapter.py` translates, `client.py` speaks HTTP, `signing.py` verifies
deliveries. Nothing outside `app/issuers/` imports any of them: the registry
entry in `app/issuers/__init__.py` is the only place this package is named.
"""

from __future__ import annotations

__all__: list[str] = []
