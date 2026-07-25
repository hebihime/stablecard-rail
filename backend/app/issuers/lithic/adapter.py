"""The Lithic adapter (SPEC.md §3.2) — translation, in both directions.

A real `FIAT_RAIL` issuer, which is the half of the taxonomy `evm_deposit_mock`
cannot exercise. Everything provider-shaped stops here: `client.py` speaks HTTP,
`signing.py` verifies deliveries, and nothing above `issuers/base.py` learns that
Lithic calls a cardholder an "account holder" or a card state `PAUSED`.

Three translations are worth knowing about before reading the code:

**A cardholder is an account.** `POST /v1/account_holders` returns both an
`account_holder_token` and an `account_token`; cards are created against the
*account*, so that is what `Cardholder.cardholder_id` carries. The holder token goes
in `raw`, where it is available for a support ticket and irrelevant to everything
else. Ids are opaque either way (SPEC.md §1).

**There is no "unactivated" virtual card.** Lithic virtual cards are created `OPEN`
or `PAUSED` and are live immediately; `PENDING_ACTIVATION` and `PENDING_FULFILLMENT`
only happen to physical cards. So `activate_card` is the unfreeze path here, exactly
as `base.py` describes it, and `CardState.UNACTIVATED` never appears.

**`raw` is an allowlist, not a copy.** Sandbox card creation answers with `pan` and
`cvv`. `Card` has no field for either, but `raw` is a free dict that ends up in the
ledger's payload column — so the fields that go in are named one by one.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping
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
    CardNotFoundError,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingRejectedError,
    FundingResult,
    FundingStatus,
    IllegalCardTransitionError,
    IssuerError,
    WebhookParseError,
)
from app.issuers.lithic.client import LithicApiError, LithicClient
from app.issuers.lithic.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    TIMESTAMP_HEADER,
    WEBHOOK_ID_HEADER,
    header_value,
    signing_key,
    verify,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "lithic"

#: Provider card state -> ours. Anything absent is refused rather than guessed at:
#: mislabelling a state is a claim about whether a card can spend money.
CARD_STATES: Mapping[str, CardState] = {
    "OPEN": CardState.ACTIVE,
    "PAUSED": CardState.FROZEN,
    "CLOSED": CardState.CANCELED,
    "PENDING_ACTIVATION": CardState.UNACTIVATED,
    "PENDING_FULFILLMENT": CardState.UNACTIVATED,
}

#: Provider transaction-event type -> ours (SPEC.md §3.3). Every kind of
#: authorization normalizes to `AUTHORIZATION`; the advice and financial variants are
#: network mechanics, not different things to a ledger. `BALANCE_INQUIRY` and
#: `RETURN_REVERSAL` are deliberately absent: neither has a normalized equivalent, so
#: they arrive as `UNMAPPED` under their own label rather than as a near-miss.
TRANSACTION_EVENTS: Mapping[str, CardEventType] = {
    "AUTHORIZATION": CardEventType.AUTHORIZATION,
    "FINANCIAL_AUTHORIZATION": CardEventType.AUTHORIZATION,
    "AUTHORIZATION_ADVICE": CardEventType.AUTHORIZATION,
    "CREDIT_AUTHORIZATION": CardEventType.AUTHORIZATION,
    "FINANCIAL_CREDIT_AUTHORIZATION": CardEventType.AUTHORIZATION,
    "CREDIT_AUTHORIZATION_ADVICE": CardEventType.AUTHORIZATION,
    "AUTHORIZATION_REVERSAL": CardEventType.AUTHORIZATION_REVERSAL,
    "CLEARING": CardEventType.SETTLEMENT,
    "RETURN": CardEventType.REFUND,
}

#: Provider events about a card itself. Only `card.updated` carries a state.
CARD_LIFECYCLE_EVENTS = frozenset(
    {
        "card.created",
        "card.updated",
        "card.converted",
        "card.reissued",
        "card.renewed",
        "card.shipped",
    }
)

#: Ours -> theirs, for the three lifecycle calls. `CLOSED` is irreversible at Lithic.
TARGET_STATES: Mapping[CardState, str] = {
    CardState.ACTIVE: "OPEN",
    CardState.FROZEN: "PAUSED",
    CardState.CANCELED: "CLOSED",
}

#: Lithic requires a phone number and a postal address that SPEC.md §3.1's
#: `CreateCardholderRequest` does not carry, and inventing fields on that DTO for one
#: provider is exactly what `raw` exists to avoid. These are obvious placeholders for
#: a sandbox program with KYC exempted; a real program submits real identity data
#: through a KYC workflow, which is out of scope (SPEC.md §2).
SANDBOX_PHONE_NUMBER = "+15555550123"
SANDBOX_ADDRESS: Mapping[str, str] = {
    "address1": "1 Analytical Engine Way",
    "city": "New York",
    "state": "NY",
    "postal_code": "10128",
    "country": "USA",
}

#: Namespace for the deterministic `Idempotency-Key` on card creation.
_IDEMPOTENCY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stablecard:lithic:create-card")

#: How a funding ref is recorded on a card. The card's `memo` is the only writable
#: free-text field it has, and it is the *provider's* copy — which is what makes it
#: readable again after a crash mid-funding (docs/ARCHITECTURE.md §4.5).
_FUNDING_TAG = "fund"
_FUNDING_TAG_PATTERN = re.compile(rf"\s*\[{_FUNDING_TAG}:(?P<ref>[^\]:]+):(?P<amount>-?\d+)\]")

#: Conservative cap for a card memo; Lithic documents no maximum.
_MEMO_MAX_LENGTH = 128

__all__ = ["CARD_STATES", "PROVIDER_ID", "TRANSACTION_EVENTS", "LithicAdapter"]


class LithicAdapter(CardIssuerAdapter):
    provider_id = PROVIDER_ID
    funding_model = FundingModel.FIAT_RAIL

    def __init__(
        self,
        *,
        client: LithicClient,
        webhook_secret: str = "",
        signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._secret = webhook_secret
        self._tolerance_seconds = signature_tolerance_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        # Validate the secret here rather than per delivery: a key derived from
        # garbage fails as "invalid signature" on every genuine webhook and sends
        # whoever debugs it looking at Lithic's dashboard instead of at `.env`.
        if webhook_secret:
            signing_key(webhook_secret)

    @classmethod
    def from_settings(cls) -> LithicAdapter:
        """The factory the registry holds (see `app/issuers/__init__.py`)."""
        settings = get_settings()
        return cls(
            client=LithicClient(
                base_url=settings.lithic_api_base_url,
                api_key=settings.lithic_api_key,
                timeout=settings.lithic_request_timeout_seconds,
            ),
            webhook_secret=settings.lithic_webhook_secret,
            signature_tolerance_seconds=settings.webhook_signature_tolerance_seconds,
        )

    # --------------------------------------------------------- cardholders ----

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        payload: dict[str, Any] = {
            "workflow": "KYC_EXEMPT",
            "kyc_exemption_type": "PREPAID_CARD_USER",
            "first_name": req.first_name,
            "last_name": req.last_name,
            "email": req.email,
            "phone_number": SANDBOX_PHONE_NUMBER,
            "address": dict(SANDBOX_ADDRESS),
        }
        if req.external_ref:
            payload["external_id"] = req.external_ref

        holder = await self._client.post("/account_holders", json_body=payload)
        return Cardholder(
            provider_id=self.provider_id,
            # Cards are created against the account, so this is the id the rest of
            # the pipeline needs to hold on to.
            cardholder_id=_require_str(holder, "account_token"),
            email=req.email,
            state=str(holder.get("status", "")) or "active",
            created_at=_utc(holder.get("created"), fallback=self._clock()),
            raw={
                "account_holder_token": holder.get("token"),
                "external_id": holder.get("external_id"),
                "status_reasons": holder.get("status_reasons"),
            },
        )

    # --------------------------------------------------------------- cards ----

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        payload: dict[str, Any] = {
            "type": "VIRTUAL",
            "account_token": cardholder_id,
            # Live on creation: see the module docstring on `UNACTIVATED`.
            "state": "OPEN",
        }
        if req.memo:
            payload["memo"] = req.memo
        if req.spend_limit_minor is not None:
            payload["spend_limit"] = _checked_limit(req.spend_limit_minor)
            # FOREVER is what makes a spend limit behave like a balance. A MONTHLY or
            # TRANSACTION window resets, and funding would leak away with it.
            payload["spend_limit_duration"] = "FOREVER"

        card = await self._client.post(
            "/cards", json_body=payload, idempotency_key=_create_card_key(cardholder_id, req)
        )
        return self._to_card(card)

    async def get_card(self, card_id: str) -> Card:
        return self._to_card(await self._read_card(card_id))

    async def activate_card(self, card_id: str) -> Card:
        return await self._transition(card_id, CardState.ACTIVE)

    async def freeze_card(self, card_id: str) -> Card:
        return await self._transition(card_id, CardState.FROZEN)

    async def cancel_card(self, card_id: str) -> Card:
        return await self._transition(card_id, CardState.CANCELED)

    async def _transition(self, card_id: str, target: CardState) -> Card:
        try:
            updated = await self._client.patch(
                f"/cards/{card_id}", json_body={"state": TARGET_STATES[target]}
            )
        except LithicApiError as exc:
            if exc.status == 405:
                # "Card not active": a closed card cannot be changed at all. That is
                # a caller mistake (409), not a provider failure (502) — but naming
                # the state it is actually in costs one extra read.
                raise await self._illegal_transition(card_id, target, exc) from exc
            raise self._translate(card_id, exc) from exc
        return self._to_card(updated)

    async def _illegal_transition(
        self, card_id: str, target: CardState, original: LithicApiError
    ) -> IssuerError:
        try:
            current = await self.get_card(card_id)
        except IssuerError:
            # No idea what state it is in, so do not invent one.
            return original
        return IllegalCardTransitionError(card_id, current.state, target)

    async def _read_card(self, card_id: str) -> dict[str, Any]:
        try:
            return await self._client.get(f"/cards/{card_id}")
        except LithicApiError as exc:
            raise self._translate(card_id, exc) from exc

    @staticmethod
    def _translate(card_id: str, exc: LithicApiError) -> IssuerError:
        """Provider failure -> the vocabulary in `issuers/base.py`.

        400 is included deliberately: Lithic answers it for a token it cannot parse
        as a UUID. Provider ids are opaque to us, so we cannot check the shape
        ourselves, and "not a token of ours" and "no such card" are the same fact.
        """
        if exc.status in (400, 404):
            return CardNotFoundError(card_id)
        return exc

    def _to_card(self, card: Mapping[str, Any]) -> Card:
        state = _require_str(card, "state")
        if state not in CARD_STATES:
            raise IssuerError(
                f"lithic reported card state {state!r}, which this adapter does not map. "
                f"Calling it something else would be a claim about whether the card can spend."
            )
        return Card(
            provider_id=self.provider_id,
            card_id=_require_str(card, "token"),
            cardholder_id=_require_str(card, "account_token"),
            state=CARD_STATES[state],
            last_four=_require_str(card, "last_four"),
            exp_month=_as_int(card.get("exp_month"), "exp_month"),
            exp_year=_as_int(card.get("exp_year"), "exp_year"),
            currency=str(card.get("cardholder_currency") or "USD"),
            spend_limit_minor=_as_optional_int(card.get("spend_limit")),
            # A fiat rail has no deposit address to hand out (SPEC.md §3.2).
            deposit_address=None,
            created_at=_utc(card.get("created"), fallback=self._clock()),
            # Named one at a time: `pan` and `cvv` are in the response and must not
            # reach a DTO, a log or the ledger.
            raw={
                "memo": card.get("memo"),
                "provider_state": state,
                "spend_limit_duration": card.get("spend_limit_duration"),
                "card_type": card.get("type"),
                "pin_status": card.get("pin_status"),
                "substatus": card.get("substatus"),
                "card_program_token": card.get("card_program_token"),
            },
        )

    # --------------------------------------------------------------- money ----

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        """Fund by raising the card's lifetime spend limit.

        This program has no ledger to move money in: `POST /v1/financial_accounts`
        answers 403 and `GET /v1/balances` answers 400 "Account does not support this
        operation", so book transfers — the production path — are unavailable. The
        one per-card dial that exists is the spend limit
        (docs/ARCHITECTURE.md §4.4).

        Idempotency comes from the memo tag, because Lithic's `Idempotency-Key`
        covers card creation and not `PATCH`. The provider holds the record, which is
        what makes it survive our crash mid-call; what it cannot do is remember an
        older ref once a newer funding has replaced the tag.
        """
        card = await self._read_card(card_id)
        state = CARD_STATES.get(_require_str(card, "state"))
        limit = _as_optional_int(card.get("spend_limit"))
        memo = str(card.get("memo") or "")
        currency = str(card.get("cardholder_currency") or "USD")

        applied = _read_funding_tag(memo)
        if applied is not None and applied[0] == funding_ref:
            if applied[1] != amount.amount_minor:
                raise FundingRejectedError(
                    card_id,
                    f"funding ref {funding_ref!r} was already applied for "
                    f"{applied[1]} minor units, not {amount.amount_minor}",
                )
            return self._funding_result(card_id, amount, funding_ref, limit or 0, replayed=True)

        if amount.amount_minor <= 0:
            raise FundingRejectedError(card_id, f"amount must be positive, got {amount}")
        if amount.currency != currency:
            raise FundingRejectedError(
                card_id, f"card is denominated in {currency}, not {amount.currency}"
            )
        if state is CardState.CANCELED:
            raise FundingRejectedError(card_id, "card is canceled")
        if not limit:
            raise FundingRejectedError(
                card_id,
                "card has no spend limit, which Lithic reads as unlimited; raising it "
                "would replace an unlimited card with a limited one",
            )

        raised = limit + amount.amount_minor
        await self._client.patch(
            f"/cards/{card_id}",
            json_body={
                "spend_limit": raised,
                # Restated every time: a limit on any other window is not a balance.
                "spend_limit_duration": "FOREVER",
                "memo": _tag_memo(memo, funding_ref, amount.amount_minor),
            },
        )
        return self._funding_result(card_id, amount, funding_ref, raised, before=limit)

    def _funding_result(
        self,
        card_id: str,
        amount: Money,
        funding_ref: str,
        after: int,
        *,
        before: int | None = None,
        replayed: bool = False,
    ) -> FundingResult:
        return FundingResult(
            provider_id=self.provider_id,
            card_id=card_id,
            funding_ref=funding_ref,
            # There is no provider-side funding object to name, so the reference is
            # the provider-side *result*: this card at this limit. That is what a
            # reconciler can actually go and check.
            issuer_funding_ref=f"{card_id}:{after}",
            status=FundingStatus.SUCCEEDED,
            amount=amount,
            raw={
                "spend_limit_before": before if before is not None else after,
                "spend_limit_after": after,
                "replayed": replayed,
            },
        )

    async def get_balance(self, card_id: str) -> Money:
        """Available spend: the card's limit, minus what it is holding and has spent.

        Lithic exposes no per-card balance for this program, and the numbers to
        derive one from are on the transactions: a pending authorization sits in
        `amounts.hold` and a settled one in `amounts.cardholder`, both negative.
        Summing them and adding the limit is the whole calculation — and it must read
        every page, or a busy card reports a balance that is too high.
        """
        card = await self._read_card(card_id)
        limit = _as_optional_int(card.get("spend_limit"))
        if not limit:
            raise IssuerError(
                f"card {card_id} has no spend limit, which Lithic reads as unlimited; "
                f"there is no available balance to report"
            )
        transactions = await self._client.list_all("/transactions", params={"card_token": card_id})
        return Money(
            limit + sum(_transaction_impact(entry) for entry in transactions),
            str(card.get("cardholder_currency") or "USD"),
        )

    # ------------------------------------------------------------ webhooks ----

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        if not self._secret:
            # Fail closed. A program with no event subscription yet has no secret, and
            # accepting unverifiable deliveries would let anyone write to the ledger.
            logger.warning("lithic webhook rejected: LITHIC_WEBHOOK_SECRET is not set")
            return False
        return verify(
            self._secret,
            headers=headers,
            body=body,
            now=self._clock(),
            tolerance_seconds=self._tolerance_seconds,
        )

    def webhook_event_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        # Inside the signed content, so a replay cannot be relabelled as a new event.
        return header_value(headers, WEBHOOK_ID_HEADER)

    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        """Normalize one delivery.

        The body is the event *payload* — Lithic sends no envelope — so the id and,
        for payloads with no timestamp of their own, the time come from the headers
        (docs/ARCHITECTURE.md §4.1).
        """
        event_id = self.webhook_event_id(headers, body)
        if not event_id:
            raise WebhookParseError(
                f"delivery has no {WEBHOOK_ID_HEADER} header, so it has no id to dedup on"
            )
        payload = _read_payload(body)
        provider_type = payload.get("event_type")
        if not isinstance(provider_type, str) or not provider_type:
            raise WebhookParseError("delivery payload has no string `event_type`")

        occurred_at = _utc(
            payload.get("updated") or payload.get("created"),
            fallback=_header_time(headers, fallback=self._clock()),
        )
        common: dict[str, Any] = {
            "provider_id": self.provider_id,
            "event_id": event_id,
            "provider_event_type": provider_type,
            "occurred_at": occurred_at,
            "card_id": _optional_str(payload.get("card_token")),
            "cardholder_id": _optional_str(payload.get("account_token")),
            # Untouched: normalizing loses nothing (SPEC.md §7 stores it).
            "raw": payload,
        }

        if provider_type == "card_transaction.updated":
            return _transaction_event(common, payload)
        if provider_type in CARD_LIFECYCLE_EVENTS:
            return CardEvent(
                **common,
                event_type=CardEventType.CARD_LIFECYCLE,
                # Only `card.updated` carries one, and an unrecognized value stays
                # unset rather than being guessed at.
                card_state=CARD_STATES.get(str(payload.get("state"))),
            )
        if provider_type == "three_ds_authentication.challenge":
            return _three_ds_event(common, payload)
        if provider_type == "dispute.updated":
            return CardEvent(
                **common,
                event_type=CardEventType.CHARGEBACK,
                amount=_dispute_amount(payload),
            )
        # Never dropped: recorded under the provider's own label (SPEC.md §3.3).
        return CardEvent(**common, event_type=CardEventType.UNMAPPED)


# --------------------------------------------------------------- webhooks ----


def _read_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebhookParseError(f"delivery body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookParseError(
            f"delivery body must be a JSON object, got {type(payload).__name__}"
        )
    return payload


def _header_time(headers: Mapping[str, str], *, fallback: datetime) -> datetime:
    """The delivery time from `webhook-timestamp`, for payloads that carry none.

    Note what this is: when Lithic *sent* the attempt, not when the event happened.
    It is only reached for payloads with no timestamp at all (`card.created`), and it
    is a better answer than our own clock because it predates our queueing.
    """
    raw = header_value(headers, TIMESTAMP_HEADER)
    if raw is None:
        return fallback
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except ValueError:
        return fallback


def _transaction_event(common: dict[str, Any], payload: Mapping[str, Any]) -> CardEvent:
    """A `card_transaction.updated`, keyed on the newest entry in `events`.

    Lithic re-sends the whole transaction every time it changes, so the same event
    type carries an authorization, then its clearing, then a reversal. The transaction
    `status` cannot stand in for that: a voided authorization reports `VOIDED` and
    would lose the reversal that caused it.
    """
    events = payload.get("events")
    latest = _newest(events) if isinstance(events, list) else None
    if latest is None:
        # Nothing to key on. Recorded as-is rather than guessed at from `status`.
        return CardEvent(**common, event_type=CardEventType.UNMAPPED)

    kind = str(latest.get("type"))
    return CardEvent(
        **{
            **common,
            "provider_event_type": f"{common['provider_event_type']}:{kind}",
            "occurred_at": _utc(latest.get("created"), fallback=common["occurred_at"]),
        },
        event_type=TRANSACTION_EVENTS.get(kind, CardEventType.UNMAPPED),
        amount=_event_amount(latest),
    )


def _newest(events: list[Any]) -> Mapping[str, Any] | None:
    """The most recent entry, by its own timestamp, falling back to array order.

    Recorded arrays are chronological, but nothing documents that they must be.
    """
    entries = [entry for entry in events if isinstance(entry, Mapping)]
    if not entries:
        return None
    return max(
        entries,
        key=lambda entry: (
            _utc(entry.get("created"), fallback=datetime.min.replace(tzinfo=UTC)),
            entries.index(entry),
        ),
    )


def _event_amount(event: Mapping[str, Any]) -> Money | None:
    """The magnitude of one transaction event, in integer minor units.

    Lithic signs these (a reversal is `-500`, a refund `-250`) and `CardEventType`
    already says which direction the money went, so a sign here would only invite
    double negation downstream. `effective_polarity` stays in `raw`.
    """
    amount = event.get("amount")
    if amount is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise WebhookParseError(
            f"transaction event amount must be integer minor units, "
            f"got {type(amount).__name__} ({amount!r})"
        )
    return Money(abs(amount), _event_currency(event))


def _event_currency(event: Mapping[str, Any]) -> str:
    amounts = event.get("amounts")
    if isinstance(amounts, Mapping):
        for part in ("cardholder", "settlement", "merchant"):
            entry = amounts.get(part)
            if isinstance(entry, Mapping) and isinstance(entry.get("currency"), str):
                return str(entry["currency"])
    return "USD"


def _three_ds_event(common: dict[str, Any], payload: Mapping[str, Any]) -> CardEvent:
    """A 3DS challenge, for the OTP service in phase 7 (SPEC.md §6).

    The amount is deliberately not normalized: Lithic states it as a decimal number
    plus a `currency_exponent`, money here is integer minor units only, and there is
    no recorded delivery to check a conversion against. `raw` keeps their numbers.
    """
    authentication = payload.get("authentication_object")
    if not isinstance(authentication, Mapping):
        return CardEvent(**common, event_type=CardEventType.UNMAPPED)
    return CardEvent(
        **{
            **common,
            "card_id": _optional_str(authentication.get("card_token")) or common["card_id"],
            "occurred_at": _utc(authentication.get("created"), fallback=common["occurred_at"]),
        },
        event_type=CardEventType.THREE_DS_CHALLENGE,
        challenge_id=_optional_str(authentication.get("token")),
    )


def _dispute_amount(payload: Mapping[str, Any]) -> Money | None:
    amount = payload.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return None
    return Money(abs(amount), "USD")


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------- funding ----


def _tag_memo(memo: str, funding_ref: str, amount_minor: int) -> str:
    """The cardholder's label plus the funding tag, within Lithic's memo length.

    The tag replaces any previous one rather than accumulating: `memo` is a display
    field, not a log. If something has to be cut it is the label, never the tag —
    the tag is the idempotency record.
    """
    tag = f"[{_FUNDING_TAG}:{funding_ref}:{amount_minor}]"
    label = _FUNDING_TAG_PATTERN.sub("", memo).strip()
    room = _MEMO_MAX_LENGTH - len(tag) - 1
    if not label or room <= 0:
        return tag
    return f"{label[:room].strip()} {tag}"


def _read_funding_tag(memo: str) -> tuple[str, int] | None:
    """The funding ref and amount last applied to this card, if any."""
    found = _FUNDING_TAG_PATTERN.search(memo)
    if found is None:
        return None
    return found.group("ref"), int(found.group("amount"))


def _transaction_impact(transaction: Mapping[str, Any]) -> int:
    """What one transaction does to a card's available spend, in minor units.

    Lithic reports a pending authorization as a negative `hold` and a settled one as
    a negative `cardholder`; a refund is positive, and a declined or voided
    transaction is zero on both. Summing the pair therefore covers every state
    without a status table — and reading a transaction wrong must fail loudly rather
    than contribute nothing, because a plausible wrong balance funds the wrong amount.
    """
    amounts = transaction.get("amounts")
    if not isinstance(amounts, Mapping):
        raise IssuerError(f"lithic transaction {transaction.get('token')!r} has no amounts")
    total = 0
    for part in ("hold", "cardholder"):
        entry = amounts.get(part, {})
        value = entry.get("amount", 0) if isinstance(entry, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise IssuerError(
                f"lithic transaction {transaction.get('token')!r} has a "
                f"non-integer {part} amount: {value!r}"
            )
        total += value
    return total


# ---------------------------------------------------------------- reading ----


def _create_card_key(cardholder_id: str, req: CreateCardRequest) -> str:
    """A deterministic `Idempotency-Key` for one logical card creation.

    Lithic supports idempotency on exactly two endpoints, and this is the one that
    matters: a retried create must not leave a cardholder with two cards. Deriving
    the key from the request means a retry reuses it without the caller having to
    thread one through — at the cost that asking twice for a byte-identical card
    yields one card. Vary the memo to mean it.
    """
    material = f"{cardholder_id}|{req.currency}|{req.spend_limit_minor}|{req.memo}"
    return str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, material))


def _checked_limit(spend_limit_minor: int) -> int:
    if spend_limit_minor <= 0:
        raise IssuerError(
            f"lithic reads spend_limit 0 as *unlimited*, so a limit of "
            f"{spend_limit_minor} cannot be expressed; omit it to mean unlimited"
        )
    return spend_limit_minor


def _require_str(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise IssuerError(f"lithic response is missing a string {field!r}")
    return value


def _as_int(value: object, field: str) -> int:
    # Lithic sends `exp_month` and `exp_year` as zero-padded strings.
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise IssuerError(f"lithic {field} is not a number: {value!r}") from exc


def _as_optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _utc(value: object, *, fallback: datetime) -> datetime:
    """An aware UTC timestamp from whatever Lithic sent.

    Their card timestamps end in `Z`; their account-holder timestamps have no offset
    at all (`2026-07-25T06:16:47.123456`). A naive value is not a legal timestamp
    anywhere in this system, and reading it as local time would move it by hours —
    Lithic's API is UTC throughout, so that is what an absent offset means.
    """
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
