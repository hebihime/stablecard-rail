"""Ledger event type constants.

Deliberately plain strings rather than an enum: the ledger also records provider
events normalised by issuer adapters, and `unmapped` payloads whose type is not
known ahead of time (SPEC.md §3.3). A closed enum here would force every new
adapter to edit a core module — exactly what the abstraction rule forbids.

Naming: `<entity>.<past-tense-event>`, dotted, lower snake case.
"""

from __future__ import annotations

# --- funding intents (phase 1) ---------------------------------------------
INTENT_CREATED = "funding_intent.created"
INTENT_TRANSITIONED = "funding_intent.transitioned"
INTENT_RETRIED = "funding_intent.retried"
INTENT_ILLEGAL_TRANSITION = "funding_intent.illegal_transition"

# --- webhook receipt (phase 2) ---------------------------------------------
WEBHOOK_DEAD_LETTERED = "webhook.dead_lettered"

#: Namespace for everything a provider told us, as opposed to something we did.
PROVIDER_PREFIX = "provider"


def provider_event(normalized_type: str) -> str:
    """The ledger type for an inbound provider event.

    A function rather than a constant per case: the argument is a
    `CardEventType` value, so this stays correct as that vocabulary grows without
    this module having to import it.
    """
    return f"{PROVIDER_PREFIX}.{normalized_type}"


__all__ = [
    "INTENT_CREATED",
    "INTENT_ILLEGAL_TRANSITION",
    "INTENT_RETRIED",
    "INTENT_TRANSITIONED",
    "PROVIDER_PREFIX",
    "WEBHOOK_DEAD_LETTERED",
    "provider_event",
]
