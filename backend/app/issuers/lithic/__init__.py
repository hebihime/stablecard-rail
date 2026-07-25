"""The Lithic adapter (SPEC.md §3.2, §12.3) — a real `FIAT_RAIL` issuer.

`adapter.py` translates, `client.py` speaks HTTP, `signing.py` verifies deliveries.
Nothing outside `app/issuers/` imports any of them: the registry entry in
`app/issuers/__init__.py` is the only place this package is named.
"""

from __future__ import annotations

from app.issuers.lithic.adapter import PROVIDER_ID, LithicAdapter

__all__ = ["PROVIDER_ID", "LithicAdapter"]
