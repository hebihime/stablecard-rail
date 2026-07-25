"""Ledger event type constants.

Deliberately plain strings rather than an enum: the ledger also records provider
events normalised by issuer adapters, and later `unmapped` payloads whose type is
not known ahead of time (SPEC.md §3.3). A closed enum here would force every new
adapter to edit a core module — exactly what the abstraction rule forbids.

Naming: `<entity>.<past-tense-event>`, dotted, lower snake case.
"""

from __future__ import annotations

# --- funding intents (phase 1) ---------------------------------------------
INTENT_CREATED = "funding_intent.created"
INTENT_TRANSITIONED = "funding_intent.transitioned"
INTENT_RETRIED = "funding_intent.retried"
INTENT_ILLEGAL_TRANSITION = "funding_intent.illegal_transition"

__all__ = [
    "INTENT_CREATED",
    "INTENT_ILLEGAL_TRANSITION",
    "INTENT_RETRIED",
    "INTENT_TRANSITIONED",
]
