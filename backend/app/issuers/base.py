"""The issuer abstraction (SPEC.md §3) — the one module every provider shares.

Adding an issuer is one adapter file plus one registry entry. Nothing in
`funding/`, `ledger/`, `webhooks/` or the mobile client may import an adapter;
they depend on this interface and on the normalized `CardEvent`, which is what
makes that promise structural rather than aspirational (enforced by
`tests/test_module_boundaries.py`).

Two rules shape the models here:

* **Money is integer minor units**, always — `app.core.money.Money`. No provider's
  decimal string reaches the rest of the system.
* **Provider identifiers are opaque strings.** We never parse them, infer meaning
  from their shape, or generate them ourselves.

Provider-specific fields live in `raw`. When a normalized field would only ever
be populated by one provider, it belongs in `raw`, not in this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.core.money import Money

__all__ = [
    "Card",
    "CardEvent",
    "CardEventType",
    "CardIssuerAdapter",
    "CardNotFoundError",
    "CardState",
    "Cardholder",
    "CardholderNotFoundError",
    "CreateCardRequest",
    "CreateCardholderRequest",
    "FundingModel",
    "FundingRejectedError",
    "FundingResult",
    "FundingStatus",
    "IllegalCardTransitionError",
    "IssuerError",
    "WebhookParseError",
]


# --------------------------------------------------------------- taxonomy ----


class FundingModel(StrEnum):
    """How money reaches a card at this provider (SPEC.md §3.2).

    `FIAT_RAIL` issuers debit a program balance funded by bank transfer.
    `CRYPTO_DEPOSIT` issuers assign an on-chain deposit address and credit the
    card when a token transfer to it confirms. The distinction changes what
    `fund_card` means, so the pipeline needs to be able to ask.
    """

    FIAT_RAIL = "fiat_rail"
    CRYPTO_DEPOSIT = "crypto_deposit"


class CardState(StrEnum):
    """Provider-side card state. Not our state machine — theirs."""

    UNACTIVATED = "unactivated"
    ACTIVE = "active"
    FROZEN = "frozen"
    CANCELED = "canceled"


class CardEventType(StrEnum):
    """The normalized event vocabulary (SPEC.md §3.3).

    `UNMAPPED` is the escape hatch that keeps the set closed: a provider event we
    do not model is recorded under its own label in `raw`, never dropped.
    """

    AUTHORIZATION = "authorization"
    AUTHORIZATION_REVERSAL = "authorization_reversal"
    SETTLEMENT = "settlement"
    REFUND = "refund"
    CHARGEBACK = "chargeback"
    THREE_DS_CHALLENGE = "three_ds_challenge"
    CARD_LIFECYCLE = "card_lifecycle"
    UNMAPPED = "unmapped"


class FundingStatus(StrEnum):
    """Outcome of a `fund_card` call.

    `PENDING` exists for providers that acknowledge synchronously and confirm by
    webhook; the funding engine treats it as "not yet funded" and waits.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# ----------------------------------------------------------------- errors ----


class IssuerError(Exception):
    """Base class for every failure originating at a provider or its adapter."""


class CardNotFoundError(IssuerError):
    def __init__(self, card_id: str) -> None:
        super().__init__(f"no such card at this provider: {card_id!r}")
        self.card_id = card_id


class CardholderNotFoundError(IssuerError):
    def __init__(self, cardholder_id: str) -> None:
        super().__init__(f"no such cardholder at this provider: {cardholder_id!r}")
        self.cardholder_id = cardholder_id


class IllegalCardTransitionError(IssuerError):
    """The provider refuses this lifecycle change (e.g. reviving a canceled card)."""

    def __init__(self, card_id: str, frm: CardState, to: CardState) -> None:
        super().__init__(f"card {card_id} cannot go from {frm} to {to}")
        self.card_id = card_id
        self.from_state = frm
        self.to_state = to


class FundingRejectedError(IssuerError):
    """The provider will not fund this card with this amount.

    Distinct from a *failed* funding: nothing was attempted, so the caller may
    fix the request and try again with the same `funding_ref`.
    """

    def __init__(self, card_id: str, reason: str) -> None:
        super().__init__(f"funding rejected for card {card_id}: {reason}")
        self.card_id = card_id
        self.reason = reason


class WebhookParseError(IssuerError):
    """An authentic delivery whose body cannot be read.

    The receiver ledgers these as `unmapped` rather than retrying: the signature
    already proved the delivery is genuine, so re-sending it changes nothing.
    """


# ----------------------------------------------------------------- models ----


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware; naive values are ambiguous")
    return value.astimezone(UTC)


#: Every timestamp crossing this boundary is stored and compared in UTC.
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class _Frozen(BaseModel):
    # frozen: these are records of what a provider said, not working state.
    # extra="forbid": a provider-specific field must go in `raw` rather than
    # quietly widening a model every other adapter has to honour.
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreateCardholderRequest(_Frozen):
    email: str
    first_name: str
    last_name: str
    #: Our own reference, echoed back by providers that support it.
    external_ref: str | None = None


class Cardholder(_Frozen):
    provider_id: str
    cardholder_id: str
    email: str
    state: str = "active"
    created_at: UtcDatetime
    raw: dict[str, Any] = Field(default_factory=dict)


class CreateCardRequest(_Frozen):
    currency: str = "USD"
    spend_limit_minor: int | None = None
    memo: str | None = None


class Card(_Frozen):
    provider_id: str
    card_id: str
    cardholder_id: str
    state: CardState
    #: The only card-number material we hold. Full PAN/CVV is a separate
    #: short-lived single-use reveal path (SPEC.md §9.2, phase 8).
    last_four: str
    exp_month: int
    exp_year: int
    currency: str
    spend_limit_minor: int | None = None
    #: Where a `CRYPTO_DEPOSIT` provider expects funds. `None` for fiat rails.
    deposit_address: str | None = None
    created_at: UtcDatetime
    raw: dict[str, Any] = Field(default_factory=dict)


class FundingResult(_Frozen):
    provider_id: str
    card_id: str
    #: Our idempotency key — the funding intent id (SPEC.md §5.2 step 3).
    funding_ref: str
    #: The provider's own reference, for reconciliation and support.
    issuer_funding_ref: str
    status: FundingStatus
    amount: Money
    raw: dict[str, Any] = Field(default_factory=dict)


class CardEvent(_Frozen):
    """A provider event, normalized (SPEC.md §3.3).

    `event_id` is the provider's id for the delivery and the basis of webhook
    dedup. `raw` always holds the untouched payload, so nothing is lost by
    normalizing — including for `UNMAPPED` events, whose provider label is kept
    in `provider_event_type`.
    """

    provider_id: str
    event_id: str
    event_type: CardEventType
    occurred_at: UtcDatetime
    #: The provider's own label, kept for `UNMAPPED` and for debugging.
    provider_event_type: str | None = None
    card_id: str | None = None
    cardholder_id: str | None = None
    amount: Money | None = None
    #: Links a settlement back to the funding intent that caused it.
    funding_ref: str | None = None
    #: Populated for `CARD_LIFECYCLE`.
    card_state: CardState | None = None
    #: Populated for `THREE_DS_CHALLENGE` (consumed by the OTP service, phase 7).
    challenge_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- adapter ----


class CardIssuerAdapter(ABC):
    """What every issuer must look like from the outside (SPEC.md §3.1).

    Implementations are stateless with respect to our data: they talk to a
    provider and translate. Nothing here touches the database, the ledger or the
    funding state machine — those are the caller's business.
    """

    #: Registry key. Opaque, stable, and stored on funding intents and ledger rows.
    provider_id: str
    funding_model: FundingModel

    @abstractmethod
    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder: ...

    @abstractmethod
    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card: ...

    @abstractmethod
    async def get_card(self, card_id: str) -> Card:
        """Current provider-side view of a card.

        Not in SPEC.md §3.1's list, but the freeze/unfreeze toggle and card screen
        (§9.1) need to read state without mutating it. See docs/ARCHITECTURE.md §3.3.
        """

    @abstractmethod
    async def activate_card(self, card_id: str) -> Card:
        """Activate — also the unfreeze path, per SPEC.md §9.1's toggle."""

    @abstractmethod
    async def freeze_card(self, card_id: str) -> Card: ...

    @abstractmethod
    async def cancel_card(self, card_id: str) -> Card: ...

    @abstractmethod
    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        """Move `amount` onto the card, idempotently under `funding_ref`.

        Contract every adapter owes the funding engine (SPEC.md §10): calling
        twice with the same `funding_ref` funds once and returns the same result,
        because the engine may retry without knowing whether the first call
        landed.
        """

    @abstractmethod
    async def get_balance(self, card_id: str) -> Money: ...

    @abstractmethod
    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Authenticate a raw delivery. Returns False rather than raising.

        Must be computed over the *raw* body: re-serializing JSON changes bytes
        and breaks every provider's signature scheme.
        """

    @abstractmethod
    async def parse_webhook(self, body: bytes) -> CardEvent:
        """Normalize a verified delivery.

        Unknown provider event types map to `CardEventType.UNMAPPED`. Raise
        `WebhookParseError` only when the body itself is unreadable.
        """

    def webhook_event_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        """Dedup id read from the envelope, before parsing (SPEC.md §4).

        Not abstract: adapters whose provider offers no envelope id inherit this
        and the receiver falls back to a digest of the body. Whatever is returned
        must be covered by the signature, or the dedup key is forgeable.
        """
        return None
