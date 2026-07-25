"""The `evm_deposit_mock` adapter (SPEC.md §3.2).

Models the crypto-funded issuer pattern: the provider assigns an EVM deposit
address per card, and funding means a confirmed token transfer to that address.
It exists to prove the abstraction spans both funding models — if the interface
only fitted fiat-rail issuers like Lithic and Stripe, that would show up here as
friction rather than in a design review.

Everything provider-side lives in `simulator.py`. This file is what a real
adapter is: translation, in both directions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.money import Money
from app.issuers.base import (
    Card,
    CardEvent,
    CardEventType,
    Cardholder,
    CardIssuerAdapter,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingResult,
    WebhookParseError,
)
from app.issuers.evm_deposit_mock.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    EVENT_ID_HEADER,
    header_value,
    verify,
)
from app.issuers.evm_deposit_mock.simulator import (
    PROVIDER_ID,
    CardRecord,
    MockIssuerSimulator,
)

#: Provider event label -> our normalized type (SPEC.md §3.3). Anything absent
#: from this map becomes `UNMAPPED`; it is never dropped.
EVENT_TYPE_MAP: Mapping[str, CardEventType] = {
    "card.authorization": CardEventType.AUTHORIZATION,
    "card.authorization_reversal": CardEventType.AUTHORIZATION_REVERSAL,
    "card.settlement": CardEventType.SETTLEMENT,
    "card.refund": CardEventType.REFUND,
    "card.chargeback": CardEventType.CHARGEBACK,
    "card.three_ds_challenge": CardEventType.THREE_DS_CHALLENGE,
    "card.state_changed": CardEventType.CARD_LIFECYCLE,
}


class EvmDepositMockAdapter(CardIssuerAdapter):
    provider_id = PROVIDER_ID
    funding_model = FundingModel.CRYPTO_DEPOSIT

    def __init__(
        self,
        *,
        webhook_secret: str,
        signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tolerance_seconds = signature_tolerance_seconds
        self._secret = webhook_secret
        self._simulator = MockIssuerSimulator(webhook_secret=webhook_secret, clock=self._clock)

    @classmethod
    def from_settings(cls) -> EvmDepositMockAdapter:
        """The factory the registry holds (see `app/issuers/__init__.py`)."""
        settings = get_settings()
        return cls(
            webhook_secret=settings.evm_deposit_mock_webhook_secret,
            signature_tolerance_seconds=settings.webhook_signature_tolerance_seconds,
        )

    @property
    def simulator(self) -> MockIssuerSimulator:
        """The fake provider. Demos and tests drive it; the pipeline never does."""
        return self._simulator

    # ----------------------------------------------------------- lifecycle ----

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        record = self._simulator.create_cardholder(
            email=req.email,
            first_name=req.first_name,
            last_name=req.last_name,
            external_ref=req.external_ref,
        )
        return Cardholder(
            provider_id=self.provider_id,
            cardholder_id=record.cardholder_id,
            email=record.email,
            created_at=record.created_at,
            raw={"external_ref": record.external_ref},
        )

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        record = self._simulator.create_card(
            cardholder_id, currency=req.currency, spend_limit_minor=req.spend_limit_minor
        )
        return self._to_card(record, memo=req.memo)

    async def get_card(self, card_id: str) -> Card:
        return self._to_card(self._simulator.get_card(card_id))

    async def activate_card(self, card_id: str) -> Card:
        return self._to_card(self._simulator.set_card_state(card_id, CardState.ACTIVE))

    async def freeze_card(self, card_id: str) -> Card:
        return self._to_card(self._simulator.set_card_state(card_id, CardState.FROZEN))

    async def cancel_card(self, card_id: str) -> Card:
        return self._to_card(self._simulator.set_card_state(card_id, CardState.CANCELED))

    async def get_balance(self, card_id: str) -> Money:
        return self._simulator.balance(card_id)

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        return self._simulator.fund(card_id, amount, funding_ref)

    def _to_card(self, record: CardRecord, *, memo: str | None = None) -> Card:
        return Card(
            provider_id=self.provider_id,
            card_id=record.card_id,
            cardholder_id=record.cardholder_id,
            state=record.state,
            last_four=record.last_four,
            exp_month=record.exp_month,
            exp_year=record.exp_year,
            currency=record.currency,
            spend_limit_minor=record.spend_limit_minor,
            deposit_address=record.deposit_address,
            created_at=record.created_at,
            raw={"memo": memo, "balance_minor": record.balance_minor},
        )

    # ------------------------------------------------------------ webhooks ----

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return verify(
            self._secret,
            headers=headers,
            body=body,
            now=self._clock(),
            tolerance_seconds=self._tolerance_seconds,
        )

    def webhook_event_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        # Read from the envelope, and covered by the signature — so the dedup key
        # cannot be rewritten by whoever replays the delivery.
        return header_value(headers, EVENT_ID_HEADER)

    async def parse_webhook(self, body: bytes) -> CardEvent:
        envelope = _read_envelope(body)
        data = envelope.data

        return CardEvent(
            provider_id=self.provider_id,
            event_id=envelope.event_id,
            event_type=EVENT_TYPE_MAP.get(envelope.provider_type, CardEventType.UNMAPPED),
            occurred_at=envelope.occurred_at,
            provider_event_type=envelope.provider_type,
            card_id=_optional_str(data.get("card_id")),
            cardholder_id=_optional_str(data.get("cardholder_id")),
            amount=_optional_money(data),
            funding_ref=_optional_str(data.get("funding_ref")),
            card_state=_optional_card_state(data.get("state")),
            challenge_id=_optional_str(data.get("challenge_id")),
            # Untouched, so normalizing never loses anything (SPEC.md §7 stores it).
            raw=envelope.raw,
        )


@dataclass(frozen=True, slots=True)
class _Envelope:
    event_id: str
    provider_type: str
    occurred_at: datetime
    data: Mapping[str, Any]
    raw: dict[str, Any]


def _read_envelope(body: bytes) -> _Envelope:
    """Validate a delivery envelope, or refuse to guess at it.

    A body that fails here is authentic but unreadable, so the receiver records it
    as `unmapped`: re-delivery cannot fix a malformed payload, and retrying one
    forever is how a webhook queue dies.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebhookParseError(f"delivery body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookParseError(
            f"delivery body must be a JSON object, got {type(payload).__name__}"
        )

    event_id, provider_type, created = (
        payload.get("id"),
        payload.get("type"),
        payload.get("created"),
    )
    if not isinstance(event_id, str) or not event_id:
        raise WebhookParseError("delivery envelope is missing a string `id`")
    if not isinstance(provider_type, str) or not provider_type:
        raise WebhookParseError("delivery envelope is missing a string `type`")
    if not isinstance(created, str):
        raise WebhookParseError("delivery envelope is missing a string `created`")
    try:
        occurred_at = datetime.fromisoformat(created)
    except ValueError as exc:
        raise WebhookParseError(f"delivery `created` is not ISO-8601: {created!r}") from exc
    if occurred_at.tzinfo is None:
        raise WebhookParseError(f"delivery `created` has no timezone: {created!r}")

    data = payload.get("data")
    return _Envelope(
        event_id=event_id,
        provider_type=provider_type,
        occurred_at=occurred_at,
        data=data if isinstance(data, dict) else {},
        raw=payload,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_card_state(value: object) -> CardState | None:
    if not isinstance(value, str):
        return None
    try:
        return CardState(value)
    except ValueError:
        return None


def _optional_money(data: Mapping[str, Any]) -> Money | None:
    """Read an amount, or nothing. Absent is not zero."""
    amount_minor, currency = data.get("amount_minor"), data.get("currency")
    if amount_minor is None or currency is None:
        return None
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise WebhookParseError(
            f"amount_minor must be integer minor units, got {type(amount_minor).__name__}"
        )
    if not isinstance(currency, str):
        raise WebhookParseError(f"currency must be a string, got {type(currency).__name__}")
    return Money(amount_minor, currency)
