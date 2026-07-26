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

# --- card actions we initiate (phase 2) ------------------------------------
CARDHOLDER_CREATED = "cardholder.created"
CARD_CREATED = "card.created"
CARD_ACTIVATED = "card.activated"
CARD_FROZEN = "card.frozen"
CARD_CANCELED = "card.canceled"

# --- webhook receipt (phase 2) ---------------------------------------------
WEBHOOK_DEAD_LETTERED = "webhook.dead_lettered"

# --- what the chain watcher saw (phase 5) ----------------------------------
# A deposit that becomes an intent is already recorded as `funding_intent.created`.
# These two are for the money that does *not*, because "nothing happened" and "we
# decided nothing happened" are different, and only one of them can be audited.
#: A transfer touched a watched address and was not creditable — failed on chain,
#: wrong mint, outgoing, or below one minor unit.
TRANSFER_IGNORED = "chain.transfer_ignored"
#: A creditable deposit arrived at an address no card claims. Real money, no home.
DEPOSIT_UNROUTABLE = "chain.deposit_unroutable"
#: A deposit already recorded — the watcher re-observed it after a restart.
DEPOSIT_DUPLICATE = "chain.deposit_duplicate"

# --- the 3DS / OTP service (phase 7) ---------------------------------------
#: A challenge became a code the app can show. The provider's delivery is already
#: ledgered as `provider.three_ds_challenge`; this is the record of what *we* then
#: did about it, and it never carries the code (docs/ARCHITECTURE.md §11.2).
OTP_DELIVERED = "otp.delivered"
#: A challenge arrived that could not be served — expired on arrival, so far. The
#: cardholder cannot complete the payment and only the ledger would ever say why.
OTP_UNDELIVERABLE = "otp.undeliverable"

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
    "CARDHOLDER_CREATED",
    "CARD_ACTIVATED",
    "CARD_CANCELED",
    "CARD_CREATED",
    "CARD_FROZEN",
    "DEPOSIT_DUPLICATE",
    "DEPOSIT_UNROUTABLE",
    "INTENT_CREATED",
    "INTENT_ILLEGAL_TRANSITION",
    "INTENT_RETRIED",
    "INTENT_TRANSITIONED",
    "OTP_DELIVERED",
    "OTP_UNDELIVERABLE",
    "PROVIDER_PREFIX",
    "TRANSFER_IGNORED",
    "WEBHOOK_DEAD_LETTERED",
    "provider_event",
]
