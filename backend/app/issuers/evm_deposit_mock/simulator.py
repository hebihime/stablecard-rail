"""The fake provider behind the mock adapter.

For Lithic and Stripe (phases 3 and 4) everything on the far side of the adapter
belongs to someone else. For `evm_deposit_mock` it lives here: card records,
balances, and an outbox of **signed webhook deliveries** identical in shape to
what would arrive over HTTP. That is what lets the whole pipeline —
verify → dedup → parse → ledger → dispatch — run end to end with no network,
no account, and no recorded fixtures to go stale (SPEC.md §3.2).

Everything is deterministic. Identifiers come from per-kind counters and deposit
addresses are derived by hash, so a demo run twice produces the same ids and a
test never needs to know a random value.

State is per instance, which is why the registry keeps one adapter per process.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.money import Money
from app.issuers.base import (
    CardholderNotFoundError,
    CardNotFoundError,
    CardState,
    FundingRejectedError,
    FundingResult,
    FundingStatus,
    IllegalCardTransitionError,
    IssuerError,
)
from app.issuers.evm_deposit_mock.signing import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)

PROVIDER_ID = "evm_deposit_mock"

#: The provider's own card lifecycle. Self-transitions are absent on purpose:
#: cancelling a canceled card is a caller bug worth surfacing, not a no-op.
CARD_TRANSITIONS: Mapping[CardState, frozenset[CardState]] = {
    CardState.UNACTIVATED: frozenset({CardState.ACTIVE, CardState.CANCELED}),
    CardState.ACTIVE: frozenset({CardState.FROZEN, CardState.CANCELED}),
    CardState.FROZEN: frozenset({CardState.ACTIVE, CardState.CANCELED}),
    CardState.CANCELED: frozenset(),
}

#: The provider's event vocabulary. Deliberately *not* our normalized names —
#: `card.state_changed` versus `card_lifecycle` — so the adapter has real
#: translation to do (SPEC.md §3.3).
PROVIDER_EVENT_TYPES = {
    "authorization": "card.authorization",
    "authorization_reversal": "card.authorization_reversal",
    "settlement": "card.settlement",
    "refund": "card.refund",
    "chargeback": "card.chargeback",
    "three_ds_challenge": "card.three_ds_challenge",
    "card_lifecycle": "card.state_changed",
}


@dataclass(frozen=True, slots=True)
class Delivery:
    """A signed webhook delivery, exactly as it would arrive over HTTP."""

    provider_id: str
    event_id: str
    #: The provider's own type label, pre-normalization.
    event_type: str
    headers: dict[str, str]
    body: bytes


@dataclass(slots=True)
class CardholderRecord:
    cardholder_id: str
    email: str
    first_name: str
    last_name: str
    external_ref: str | None
    created_at: datetime


@dataclass(slots=True)
class CardRecord:
    card_id: str
    cardholder_id: str
    state: CardState
    last_four: str
    exp_month: int
    exp_year: int
    currency: str
    spend_limit_minor: int | None
    #: Where the provider expects funds — the `CRYPTO_DEPOSIT` funding model.
    deposit_address: str
    balance_minor: int
    created_at: datetime


@dataclass(slots=True)
class AuthorizationRecord:
    authorization_id: str
    card_id: str
    amount: Money
    merchant: str


def deposit_address_for(card_id: str, *, program: str) -> str:
    """Derive the card's EVM deposit address.

    Deterministic and obviously synthetic: a real provider would hand back an
    address from its own key material. 20 bytes, hex, `0x`-prefixed, so it is the
    right *shape* for the chain code in phase 5 to handle.
    """
    digest = hashlib.sha256(f"{program}:{card_id}".encode()).hexdigest()
    return f"0x{digest[:40]}"


class MockIssuerSimulator:
    """In-process stand-in for a crypto-funded card issuer."""

    def __init__(
        self,
        *,
        webhook_secret: str,
        clock: Callable[[], datetime] | None = None,
        program: str = "stablecard-demo",
    ) -> None:
        self._secret = webhook_secret
        self._clock = clock or (lambda: datetime.now(UTC))
        self._program = program
        self._counters: dict[str, int] = {}
        self._cardholders: dict[str, CardholderRecord] = {}
        self._cards: dict[str, CardRecord] = {}
        self._authorizations: dict[str, AuthorizationRecord] = {}
        #: `funding_ref` -> the result returned the first time. The whole of
        #: `fund_card` idempotency (SPEC.md §10) is this dictionary.
        self._fundings: dict[str, FundingResult] = {}
        self._deliveries: list[Delivery] = []

    # ------------------------------------------------------------ helpers ----

    def _next(self, kind: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{kind}_{self._counters[kind]:06d}"

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _require_card(self, card_id: str) -> CardRecord:
        card = self._cards.get(card_id)
        if card is None:
            raise CardNotFoundError(card_id)
        return card

    # ----------------------------------------------------------- lifecycle ----

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(self._cards)

    def create_cardholder(
        self, *, email: str, first_name: str, last_name: str, external_ref: str | None = None
    ) -> CardholderRecord:
        record = CardholderRecord(
            cardholder_id=self._next("chr"),
            email=email,
            first_name=first_name,
            last_name=last_name,
            external_ref=external_ref,
            created_at=self._now(),
        )
        self._cardholders[record.cardholder_id] = record
        return record

    def get_cardholder(self, cardholder_id: str) -> CardholderRecord:
        holder = self._cardholders.get(cardholder_id)
        if holder is None:
            raise CardholderNotFoundError(cardholder_id)
        return holder

    def create_card(
        self, cardholder_id: str, *, currency: str, spend_limit_minor: int | None = None
    ) -> CardRecord:
        self.get_cardholder(cardholder_id)
        created_at = self._now()
        card_id = self._next("card")
        sequence = self._counters["card"]
        card = CardRecord(
            card_id=card_id,
            cardholder_id=cardholder_id,
            state=CardState.UNACTIVATED,
            # Synthetic and repeating on purpose: nothing here should ever be
            # mistaken for card-number material.
            last_four=f"{(sequence * 1111) % 10_000:04d}",
            exp_month=created_at.month,
            exp_year=created_at.year + 3,
            currency=currency.upper(),
            spend_limit_minor=spend_limit_minor,
            deposit_address=deposit_address_for(card_id, program=self._program),
            balance_minor=0,
            created_at=created_at,
        )
        self._cards[card_id] = card
        return card

    def get_card(self, card_id: str) -> CardRecord:
        return self._require_card(card_id)

    def set_card_state(self, card_id: str, target: CardState) -> CardRecord:
        card = self._require_card(card_id)
        if target not in CARD_TRANSITIONS[card.state]:
            raise IllegalCardTransitionError(card_id, card.state, target)
        card.state = target
        return card

    def balance(self, card_id: str) -> Money:
        card = self._require_card(card_id)
        return Money(card.balance_minor, card.currency)

    # -------------------------------------------------------------- funding ----

    def fund(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        """Credit the card once per `funding_ref`, then confirm by webhook.

        The `CRYPTO_DEPOSIT` reading of this call: our bridge has delivered tokens
        to `deposit_address` and the provider has observed the deposit, so it
        credits the card and later confirms with a settlement event.
        """
        card = self._require_card(card_id)

        previous = self._fundings.get(funding_ref)
        if previous is not None:
            # A replay. Identical terms return the original verbatim; different
            # terms are a caller bug — honouring either amount would be wrong.
            if previous.card_id != card_id or previous.amount != amount:
                raise FundingRejectedError(
                    card_id,
                    f"funding_ref {funding_ref!r} was already used for "
                    f"{previous.amount} on card {previous.card_id}",
                )
            return previous

        if card.state is CardState.CANCELED:
            raise FundingRejectedError(card_id, "card is canceled")
        if amount.currency != card.currency:
            raise FundingRejectedError(
                card_id, f"card is denominated in {card.currency}, not {amount.currency}"
            )
        if amount.amount_minor <= 0:
            raise FundingRejectedError(card_id, f"amount must be positive, got {amount}")

        card.balance_minor += amount.amount_minor
        result = FundingResult(
            provider_id=PROVIDER_ID,
            card_id=card_id,
            funding_ref=funding_ref,
            issuer_funding_ref=self._next("mockfund"),
            status=FundingStatus.SUCCEEDED,
            amount=amount,
            raw={
                "deposit_address": card.deposit_address,
                "balance_minor": card.balance_minor,
                "funding_model": "crypto_deposit",
            },
        )
        self._fundings[funding_ref] = result
        # The provider confirms asynchronously; SPEC.md §5.2 step 4 reconciles
        # the funding intent from this event.
        self.emit_settlement(card_id, amount, funding_ref=funding_ref)
        return result

    # ------------------------------------------------------------ webhooks ----

    @property
    def deliveries(self) -> tuple[Delivery, ...]:
        return tuple(self._deliveries)

    def drain_deliveries(self) -> tuple[Delivery, ...]:
        """Hand over everything emitted so far and forget it."""
        drained = tuple(self._deliveries)
        self._deliveries.clear()
        return drained

    def _emit(self, provider_type: str, data: dict[str, Any]) -> Delivery:
        event_id = self._next("evt")
        occurred_at = self._now()
        payload = {
            "id": event_id,
            "type": provider_type,
            "created": occurred_at.isoformat(),
            "data": data,
        }
        # sort_keys + compact separators: the bytes are what gets signed, so they
        # must be reproducible for a given payload.
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(occurred_at.timestamp()))
        headers = {
            "content-type": "application/json",
            TIMESTAMP_HEADER: timestamp,
            EVENT_ID_HEADER: event_id,
            SIGNATURE_HEADER: sign(self._secret, timestamp=timestamp, event_id=event_id, body=body),
        }
        delivery = Delivery(
            provider_id=PROVIDER_ID,
            event_id=event_id,
            event_type=provider_type,
            headers=headers,
            body=body,
        )
        self._deliveries.append(delivery)
        return delivery

    def emit_authorization(
        self,
        card_id: str,
        amount: Money,
        *,
        merchant: str = "Test Merchant",
        mcc: str = "5814",
    ) -> Delivery:
        card = self._require_card(card_id)
        authorization = AuthorizationRecord(
            authorization_id=self._next("auth"),
            card_id=card_id,
            amount=amount,
            merchant=merchant,
        )
        self._authorizations[authorization.authorization_id] = authorization
        return self._emit(
            PROVIDER_EVENT_TYPES["authorization"],
            {
                "card_id": card_id,
                "cardholder_id": card.cardholder_id,
                "authorization_id": authorization.authorization_id,
                "amount_minor": amount.amount_minor,
                "currency": amount.currency,
                "merchant": {"name": merchant, "mcc": mcc},
            },
        )

    def emit_authorization_reversal(self, authorization_id: str) -> Delivery:
        authorization = self._authorizations.get(authorization_id)
        if authorization is None:
            raise IssuerError(f"no such authorization at this provider: {authorization_id!r}")
        card = self._require_card(authorization.card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["authorization_reversal"],
            {
                "card_id": authorization.card_id,
                "cardholder_id": card.cardholder_id,
                "authorization_id": authorization_id,
                "amount_minor": authorization.amount.amount_minor,
                "currency": authorization.amount.currency,
            },
        )

    def emit_settlement(
        self,
        card_id: str,
        amount: Money,
        *,
        authorization_id: str | None = None,
        funding_ref: str | None = None,
    ) -> Delivery:
        card = self._require_card(card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["settlement"],
            {
                "card_id": card_id,
                "cardholder_id": card.cardholder_id,
                "settlement_id": self._next("stl"),
                "authorization_id": authorization_id,
                "funding_ref": funding_ref,
                "amount_minor": amount.amount_minor,
                "currency": amount.currency,
            },
        )

    def emit_refund(self, card_id: str, amount: Money) -> Delivery:
        card = self._require_card(card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["refund"],
            {
                "card_id": card_id,
                "cardholder_id": card.cardholder_id,
                "refund_id": self._next("rfd"),
                "amount_minor": amount.amount_minor,
                "currency": amount.currency,
            },
        )

    def emit_chargeback(self, card_id: str, amount: Money, *, reason: str = "fraud") -> Delivery:
        card = self._require_card(card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["chargeback"],
            {
                "card_id": card_id,
                "cardholder_id": card.cardholder_id,
                "chargeback_id": self._next("cbk"),
                "amount_minor": amount.amount_minor,
                "currency": amount.currency,
                "reason": reason,
            },
        )

    def emit_three_ds_challenge(self, card_id: str, *, code: str = "123456") -> Delivery:
        """A 3DS challenge. The OTP service consumes these in phase 7 (SPEC.md §6)."""
        card = self._require_card(card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["three_ds_challenge"],
            {
                "card_id": card_id,
                "cardholder_id": card.cardholder_id,
                "challenge_id": self._next("3ds"),
                "otp_code": code,
            },
        )

    def emit_card_lifecycle(self, card_id: str) -> Delivery:
        card = self._require_card(card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["card_lifecycle"],
            {
                "card_id": card_id,
                "cardholder_id": card.cardholder_id,
                "state": card.state.value,
            },
        )

    def emit_unknown(self, provider_type: str, data: Mapping[str, Any] | None = None) -> Delivery:
        """Emit an event type we do not model, to exercise the `unmapped` path."""
        return self._emit(provider_type, dict(data or {}))
