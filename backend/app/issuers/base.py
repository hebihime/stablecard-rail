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
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ExternalError
from app.core.money import Money
from app.core.time import UtcDatetime

__all__ = [
    "Card",
    "CardEvent",
    "CardEventType",
    "CardIssuerAdapter",
    "CardNotFoundError",
    "CardState",
    "Cardholder",
    "CardholderNotFoundError",
    "ChallengeAlreadyAnsweredError",
    "ChallengeDecision",
    "ChallengeNotFoundError",
    "ChallengeResponse",
    "ChallengeResponseUnsupported",
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


class ChallengeDecision(StrEnum):
    """What a cardholder said about a 3DS challenge (SPEC.md §6.5).

    Two values, because a challenge has two answers. Deliberately *not* the
    providers' spellings — Lithic's wire value for a decline is
    `DECLINE_BY_CUSTOMER`, and translating that is the adapter's job, exactly as it
    is for every other provider vocabulary here.
    """

    APPROVE = "approve"
    DECLINE = "decline"


class FundingStatus(StrEnum):
    """Outcome of a `fund_card` call.

    `PENDING` exists for providers that acknowledge synchronously and confirm by
    webhook; the funding engine treats it as "not yet funded" and waits.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# ----------------------------------------------------------------- errors ----


class IssuerError(ExternalError):
    """Base class for every failure originating at a provider or its adapter.

    `retryable`, inherited from `ExternalError`, answers the one question the
    funding engine has about a failure it cannot otherwise interpret: **could the
    identical call succeed if it were made again?** A 429 or a 503 says yes; a
    card that does not exist says no. The engine sees `IssuerError` and never the
    concrete type, so without a marker on this class the distinction could not
    cross `issuers/base.py` at all — each adapter knew it privately and nobody
    above could ask (docs/ARCHITECTURE.md §9.1).

    Adapters that retry internally (both HTTP clients do) have already spent
    their own attempts by the time one of these is raised.
    """


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


class ChallengeResponseUnsupported(IssuerError):
    """This provider has no way to be told what the cardholder decided.

    Not a failure of ours and not a provider outage — a **capability gap**, which
    SPEC.md §6.5 anticipates: the response is "posted back through the adapter where
    the sandbox supports it; otherwise ledgered as `responded` with the payload that
    would be sent". So the OTP service catches this and records the decision instead
    of failing the request.

    Stripe Issuing is the case that makes it necessary: it publishes no issuer-facing
    3DS challenge at all (docs/ARCHITECTURE.md §8.8), so there is nothing to respond
    to and no endpoint to respond on. Never retryable — waiting does not give a
    provider an API it does not have.
    """

    def __init__(self, provider_id: str) -> None:
        super().__init__(
            f"{provider_id} has no challenge-response endpoint; "
            f"the decision can be recorded but not delivered",
            retryable=False,
        )
        self.provider_id = provider_id


class ChallengeNotFoundError(IssuerError):
    """The provider does not recognise this challenge.

    Distinct from our own store not holding it: this is the provider saying the
    token is unknown — expired at their end, or belonging to another program.
    """

    def __init__(self, challenge_id: str) -> None:
        super().__init__(f"no such 3DS challenge at this provider: {challenge_id!r}")
        self.challenge_id = challenge_id


class ChallengeAlreadyAnsweredError(IssuerError):
    """The challenge has already been decided, by us or by a timeout.

    A 3DS challenge is single-use and short-lived, so a second answer is either a
    duplicate request or one that arrived after the ACS gave up. Never retryable —
    the outcome is already fixed, whatever it was.
    """

    def __init__(self, challenge_id: str, reason: str) -> None:
        super().__init__(f"3DS challenge {challenge_id} can no longer be answered: {reason}")
        self.challenge_id = challenge_id
        self.reason = reason


class RevealUnsupported(IssuerError):
    """This provider has no card-reveal path we are willing to use.

    A capability gap, like `ChallengeResponseUnsupported`, but the gap is partly
    ours by choice. Lithic and Stripe both *would* hand over a sandbox PAN — Lithic
    on the card object, Stripe behind `expand[]=number,cvc` — and taking it would
    route card-number material through a backend built so that none exists in it
    (docs/ARCHITECTURE.md §12.2). Gnosis Pay's PSE is the model this repo follows,
    and under PSE the partner backend never sees a number at all.

    Never retryable: waiting does not give a provider a secure-rendering path, and
    it does not change our mind about the two that have one.
    """

    def __init__(self, provider_id: str) -> None:
        super().__init__(
            f"{provider_id} has no card-reveal path; "
            f"card details are not available through this adapter",
            retryable=False,
        )
        self.provider_id = provider_id


class RevealTokenError(IssuerError):
    """A reveal could not be completed at the provider.

    Distinct from our own token being unusable, which never reaches an adapter: this
    is the provider refusing the exchange — an ephemeral token expired, replayed, or
    minted against a card the program no longer owns.
    """


class WebhookParseError(IssuerError):
    """An authentic delivery whose body cannot be read.

    The receiver ledgers these as `unmapped` rather than retrying: the signature
    already proved the delivery is genuine, so re-sending it changes nothing.
    """


# ----------------------------------------------------------------- models ----


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
    #: The provider's own reference, for reconciliation and support. `None` means
    #: *nothing has been observed yet*, which is the honest answer for a
    #: `CRYPTO_DEPOSIT` provider asked to fund before a deposit confirms: there is
    #: no provider-side object to point at. An empty string would have claimed
    #: there is one and that it is nameless. `FundingIntent.issuer_funding_ref` is
    #: nullable for the same reason, so this makes the DTO and the column agree.
    issuer_funding_ref: str | None = None
    status: FundingStatus
    amount: Money
    raw: dict[str, Any] = Field(default_factory=dict)


class RevealedCard(_Frozen):
    """What a reveal hands back — deliberately not a card number.

    SPEC.md §9.2 asks for "full PAN/CVV fetched via a short-lived, single-use reveal
    token", structured "deliberately as the Gnosis Pay PSE pattern". Those two
    requirements pull against each other, and PSE wins: under PSE the partner backend
    calls for an ephemeral token, hands it to the client, and an SDK renders the card
    inside an isolated component. The number never reaches the partner's servers,
    which is the entire point of the design.

    So this model has nowhere to put a PAN, and `tests/test_card_reveal.py` asserts
    that rather than trusting it. `rendered_in` names where the number actually
    appears, so a client can say so honestly instead of implying it has been withheld
    by accident.
    """

    provider_id: str
    card_id: str
    #: Enough to confirm *which* card is being revealed, and no more.
    last_four: str
    exp_month: int
    exp_year: int
    #: The isolated surface the real number is rendered on, named by the provider.
    #: `"pse-iframe"` for Gnosis Pay.
    rendered_in: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ChallengeResponse(_Frozen):
    """What came back from telling a provider the cardholder's decision.

    `provider_ref` is `None` for a provider that acknowledges without naming
    anything — Lithic's challenge-response endpoint answers 200 with no body at all,
    which is the honest "received, nothing to point at".
    """

    provider_id: str
    challenge_id: str
    decision: ChallengeDecision
    provider_ref: str | None = None
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
    #: When the challenge dies, if the provider says. 3DS challenges are short —
    #: Lithic's are typically ten minutes — and the provider's own deadline beats a
    #: configured default, because a code that outlives the challenge is a code the
    #: cardholder can enter and be declined for (docs/ARCHITECTURE.md §11.3).
    challenge_expires_at: UtcDatetime | None = None
    #: The one-time code, when the provider issues one rather than expecting us to.
    #:
    #: **`exclude=True`, and that is load-bearing.** A `CardEvent` is serialized
    #: into four stores — the ledger, the `EventBus` stream, the handler retry
    #: queue and the dead-letter table — and three of them outlive a five-minute
    #: code; the ledger is append-only, so a code written there cannot even be
    #: redacted afterwards. Excluding the field on the *model* is the only fix that
    #: covers a sink nobody has written yet (§11.2). The code therefore exists in
    #: memory between `parse_webhook` and the OTP handler, and nowhere else but
    #: Redis under a TTL.
    otp_code: str | None = Field(default=None, exclude=True)
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
    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        """Normalize a verified delivery.

        Takes the headers as well as the body — SPEC.md §3.1 sketches this as
        body-only, but a real provider's envelope is split across both: Lithic
        sends the event id in `webhook-id` and its `card.created` payload has no
        timestamp anywhere, so body-only normalization cannot fill in `CardEvent`
        (docs/ARCHITECTURE.md §4.1).

        Unknown provider event types map to `CardEventType.UNMAPPED`. Raise
        `WebhookParseError` only when the body itself is unreadable.
        """

    async def respond_to_challenge(
        self, challenge_id: str, decision: ChallengeDecision
    ) -> ChallengeResponse:
        """Tell the provider what the cardholder decided (SPEC.md §6.5).

        **Not abstract, and the default raises.** `webhook_event_id` set the
        precedent: an interface method that only some providers can honour belongs
        here with a default, not as an obligation every adapter has to fake. The
        alternative was worse in both directions — making it abstract would force
        Stripe's adapter to implement a method for an endpoint that does not exist,
        and leaving it off the interface entirely would mean `otp/` reaching into a
        concrete adapter to find out, which is the coupling
        `tests/test_module_boundaries.py` exists to prevent.

        So the capability gap is expressed in the type system: a provider that cannot
        accept a response raises `ChallengeResponseUnsupported`, and the caller
        ledgers the decision instead of delivering it — which is precisely the
        fallback §6.5 describes.

        Raises:
            ChallengeResponseUnsupported: this provider has no such endpoint.
            ChallengeNotFoundError: the provider does not know this challenge.
            ChallengeAlreadyAnsweredError: it has already been decided or has timed out.
        """
        raise ChallengeResponseUnsupported(self.provider_id)

    async def reveal(self, card_id: str) -> RevealedCard:
        """Exchange for the card's own details, on the provider's secure path.

        **Not abstract, and the default raises** — the third method to take this
        shape, after `webhook_event_id` and `respond_to_challenge`, and by now the
        pattern is the interface's answer to capability gaps rather than an
        exception to it.

        What an adapter does inside here is a whole provider protocol: for Gnosis Pay
        it is minting a 60-second PSE ephemeral token and redeeming it, both within
        this call, so the provider credential never crosses our API boundary. Our own
        short-lived single-use token is a separate layer that lives in `app/reveal/`
        and knows nothing about any of this.

        Raises:
            RevealUnsupported: this provider has no reveal path we use.
            CardNotFoundError: no such card at the provider.
            RevealTokenError: the provider refused the exchange.
        """
        raise RevealUnsupported(self.provider_id)

    def webhook_event_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        """Dedup id read from the envelope, before parsing (SPEC.md §4).

        Not abstract: adapters whose provider offers no envelope id inherit this
        and the receiver falls back to a digest of the body. Whatever is returned
        must be covered by the signature, or the dedup key is forgeable.
        """
        return None
