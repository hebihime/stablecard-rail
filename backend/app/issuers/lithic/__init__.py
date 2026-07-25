"""The Lithic adapter (SPEC.md §3.2, §12.3) — a real `FIAT_RAIL` issuer.

`signing.py` is their webhook scheme; the adapter itself lands with the HTTP
client. Nothing outside `app/issuers/` imports either.
"""

from __future__ import annotations

__all__: list[str] = []
