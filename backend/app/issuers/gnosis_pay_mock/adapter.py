"""The `gnosis_pay_mock` adapter (SPEC.md §3.2).

Models the Gnosis Pay partner pattern, shaped on their public documentation: a
per-user **Safe** smart account on Gnosis Chain is the card's funding source, so
funding is a confirmed stablecoin deposit to that Safe and **not an API call**.
`fund_card` therefore verifies and attributes a deposit rather than moving money —
if it could move money it would be a fiat rail with a crypto-sounding name.

It exists to prove the abstraction spans both funding models. If the interface
only fitted fiat-rail issuers like Lithic and Stripe, that would show up here as
friction rather than in a design review, and it is a faithful enough shadow of a
real provider to double as preparation for integrating one.

Everything provider-side lives in `simulator.py`. This file is what a real adapter
is: translation, in both directions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.money import Money
from app.issuers.base import (
    Card,
    CardEvent,
    CardEventType,
    Cardholder,
    CardIssuerAdapter,
    CardState,
    ChallengeDecision,
    ChallengeResponse,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingResult,
    IssuerError,
    RevealedCard,
    WebhookParseError,
)
from app.issuers.gnosis_pay_mock.config import get_gnosis_pay_mock_settings
from app.issuers.gnosis_pay_mock.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    TIMESTAMP_HEADER,
    header_value,
    verify,
)
from app.issuers.gnosis_pay_mock.simulator import (
    CHAIN_NAME,
    EPHEMERAL_TOKEN_TTL_SECONDS,
    EXTENSION_EVENT_TYPES,
    PROVIDER_EVENT_TYPES,
    PROVIDER_ID,
    SAFE_CURRENCIES,
    CardRecord,
    GnosisPaySimulator,
    SafeCurrency,
    to_money,
)

#: Provider `eventType` -> our normalized type (SPEC.md §3.3). Anything absent
#: from this map becomes `UNMAPPED`; it is never dropped. `card.transaction.cleared`
#: is deliberately absent: it is a payment, a refund or a reversal depending on
#: `kind` inside the payload, so it needs `_cleared_event_type` rather than a
#: lookup.
EVENT_TYPE_MAP: Mapping[str, CardEventType] = {
    PROVIDER_EVENT_TYPES["card_lifecycle"]: CardEventType.CARD_LIFECYCLE,
    PROVIDER_EVENT_TYPES["authorization"]: CardEventType.AUTHORIZATION,
    EXTENSION_EVENT_TYPES["three_ds_challenge"]: CardEventType.THREE_DS_CHALLENGE,
    EXTENSION_EVENT_TYPES["chargeback"]: CardEventType.CHARGEBACK,
}

#: Their transaction `kind` -> our normalized type, for `card.transaction.cleared`.
CLEARED_KIND_MAP: Mapping[str, CardEventType] = {
    "Payment": CardEventType.SETTLEMENT,
    "Refund": CardEventType.REFUND,
    "Reversal": CardEventType.AUTHORIZATION_REVERSAL,
}

#: Which field carries the amount, by `kind`. Their schema names it after the kind.
AMOUNT_FIELD_BY_KIND: Mapping[str, str] = {
    "Payment": "billingAmount",
    "Refund": "refundAmount",
    "Reversal": "reversalAmount",
}

#: What replaces the one-time code in `raw`. A marker rather than a deletion, so
#: the ledger records that a code was delivered without recording the code.
REDACTED = "[redacted]"

#: Their status names -> our provider-side card state, for `card.status.changed`.
#: The webhook carries a name, not the boolean flags the status endpoint returns.
STATUS_NAME_MAP: Mapping[str, CardState] = {
    "Inactive": CardState.UNACTIVATED,
    "Active": CardState.ACTIVE,
    "Frozen": CardState.FROZEN,
    "Void": CardState.CANCELED,
    "Lost": CardState.CANCELED,
    "Stolen": CardState.CANCELED,
}


class GnosisPayMockAdapter(CardIssuerAdapter):
    provider_id = PROVIDER_ID
    funding_model = FundingModel.CRYPTO_DEPOSIT

    def __init__(
        self,
        *,
        signing_key: Ed25519PrivateKey | None = None,
        signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tolerance_seconds = signature_tolerance_seconds
        # The key goes to the *simulator*, which is the party that signs. This
        # adapter holds no private key: `verify_webhook` reads the public half back
        # out, which is all a real partner integration ever has.
        self._simulator = GnosisPaySimulator(signing_key=signing_key, clock=self._clock)

    @classmethod
    def from_settings(cls) -> GnosisPayMockAdapter:
        """The factory the registry holds (see `app/issuers/__init__.py`).

        Reads `GnosisPayMockSettings`, which is almost empty: a simulator in this
        process has no credentials to configure. Adding this provider still cost
        no change to `app/core/config.py` — now structurally, not by luck
        (`config.py`).
        """
        settings = get_gnosis_pay_mock_settings()
        return cls(signature_tolerance_seconds=settings.signature_tolerance_seconds)

    @property
    def simulator(self) -> GnosisPaySimulator:
        """The fake provider. Demos and tests drive it; the pipeline never does."""
        return self._simulator

    # ----------------------------------------------------------- lifecycle ----

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        """A Gnosis Pay user, and the Safe that is their account.

        Upstream this is SIWE authentication plus KYC plus a Safe deployment, none
        of which the interface models — SPEC.md §2 puts real KYC out of scope. What
        matters downstream is the Safe address, because that is where money goes.
        """
        record = self._simulator.create_user(
            email=req.email,
            first_name=req.first_name,
            last_name=req.last_name,
            external_ref=req.external_ref,
        )
        return Cardholder(
            provider_id=self.provider_id,
            cardholder_id=record.user_id,
            email=record.email,
            created_at=record.created_at,
            raw={
                "externalRef": record.external_ref,
                "safeAddress": record.safe_address,
                "safeCurrency": record.currency.symbol,
                "safeDeployed": record.safe_deployed,
                "chain": CHAIN_NAME,
            },
        )

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        """`POST /api/v1/cards/virtual`.

        `req.currency` must match the Safe's: this provider denominates per Safe,
        not per card, and there is no conversion. `req.spend_limit_minor` becomes
        the Safe's on-chain **daily** limit, which every card of the user shares —
        the only limit the provider has.
        """
        user = self._simulator.get_user(cardholder_id)
        if req.currency.upper() != user.currency.code:
            raise IssuerError(
                f"the Safe is denominated in {user.currency.code} ({user.currency.symbol}), "
                f"so it cannot issue a {req.currency.upper()} card"
            )
        record = self._simulator.create_virtual_card(
            cardholder_id, spend_limit_minor=req.spend_limit_minor
        )
        return self._to_card(record, memo=req.memo)

    async def get_card(self, card_id: str) -> Card:
        return self._to_card(self._simulator.get_card(card_id))

    async def activate_card(self, card_id: str) -> Card:
        """Activate, or unfreeze.

        SPEC.md §9.1's freeze toggle is one method on the interface, and for this
        provider it is two endpoints: `/unfreeze` undoes a freeze, `/activate` only
        works once. Which one to call is exactly the sort of thing an adapter is
        for.
        """
        card = self._simulator.get_card(card_id)
        if card.card_state is CardState.FROZEN:
            return self._to_card(self._simulator.unfreeze_card(card_id))
        return self._to_card(self._simulator.activate_card(card_id))

    async def freeze_card(self, card_id: str) -> Card:
        return self._to_card(self._simulator.freeze_card(card_id))

    async def cancel_card(self, card_id: str) -> Card:
        """`POST /api/v1/cards/{cardId}/void`.

        Void is the cancellation a caller can ask for. Lost and stolen are also
        terminal at the provider, but they are reports about the physical world
        rather than an instruction, so they stay on the provider surface.
        """
        return self._to_card(self._simulator.void_card(card_id))

    async def get_balance(self, card_id: str) -> Money:
        """The Safe's spendable balance — shared by every card the user holds."""
        card = self._simulator.get_card(card_id)
        return self._simulator.spendable(card.user_id)

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        """Verify and attribute an on-chain deposit. Moves nothing.

        `PENDING` means the deposit has not confirmed yet, and the engine should
        wait rather than retry differently — there is no API call that would make
        the money arrive sooner.
        """
        return self._simulator.confirm_funding(card_id, amount, funding_ref)

    def _to_card(self, record: CardRecord, *, memo: str | None = None) -> Card:
        user = self._simulator.get_user(record.user_id)
        status = self._simulator.card_status(record.card_id)
        return Card(
            provider_id=self.provider_id,
            card_id=record.card_id,
            cardholder_id=record.user_id,
            state=record.card_state,
            last_four=record.last_four,
            exp_month=record.exp_month,
            exp_year=record.exp_year,
            currency=user.currency.code,
            # Per Safe, not per card. Reported so a caller can see the limit that
            # actually applies, with the sharing spelled out in `raw`.
            spend_limit_minor=user.daily_limit_minor,
            # One Safe per user: every card of theirs funds from this address.
            deposit_address=user.safe_address,
            created_at=record.created_at,
            raw={
                "memo": memo,
                "cardToken": record.card_token,
                "virtual": record.virtual,
                "activatedAt": status["activatedAt"],
                "statusCode": status["statusCode"],
                "statusName": record.status_name,
                "safeCurrency": user.currency.symbol,
                "dailyLimitIsPerSafe": True,
                "chain": CHAIN_NAME,
            },
        )

    # ----------------------------------------------------------- 3DS / OTP ----

    async def respond_to_challenge(
        self, challenge_id: str, decision: ChallengeDecision
    ) -> ChallengeResponse:
        """`POST /api/v1/cards/3ds/{id}/response` in shape (SPEC.md §6.5).

        An extension, like the challenge webhook itself: Gnosis Pay publishes no 3DS
        surface, and SPEC.md §6 leans on this simulator for the OTP path. Which makes
        this the one provider here where the whole round trip is exercisable —
        Lithic's needs a challenge their sandbox cannot raise, and Stripe has no
        endpoint at all.

        Translation in both directions, which is this file's job: our
        `ChallengeDecision` becomes the simulator's answer string, and the
        simulator's refusals arrive as the interface's own errors rather than as
        anything mock-shaped.
        """
        challenge = self._simulator.answer_challenge(challenge_id, decision.value)
        return ChallengeResponse(
            provider_id=self.provider_id,
            challenge_id=challenge_id,
            decision=decision,
            # Their acknowledgement names the answered challenge and the moment.
            provider_ref=challenge.challenge_id,
            raw={
                "challengeId": challenge.challenge_id,
                "response": challenge.answer,
                "answeredAt": challenge.answered_at.isoformat() if challenge.answered_at else None,
            },
        )

    # -------------------------------------------------------------- reveal ----

    async def reveal(self, card_id: str) -> RevealedCard:
        """`POST /api/v1/ephemeral-token`, then the PSE exchange (SPEC.md §9.2).

        Both halves happen here, in one call, and that is the design rather than a
        shortcut. Upstream the mint is authenticated by **mTLS with the partner's app
        id in the certificate CN** — a mobile client cannot make it, has no
        certificate, and should never be handed a credential minted with ours. So the
        ephemeral token is born and dies inside this method, and what leaves is a
        `RevealedCard` carrying no token and no number.

        The single-use property therefore protects the provider call, not the
        cardholder-facing screen: our own token in `app/reveal/` is what makes the
        *screen* single-use. Two layers, two different things being protected, and
        conflating them would mean either replayable provider credentials or a
        cardholder who cannot reopen a screen they just closed.
        """
        minted = self._simulator.mint_ephemeral_token(card_id)
        rendered = self._simulator.redeem_ephemeral_token(minted["data"]["token"])
        return RevealedCard(
            provider_id=self.provider_id,
            card_id=rendered["cardId"],
            last_four=rendered["lastFourDigits"],
            exp_month=rendered["expMonth"],
            exp_year=rendered["expYear"],
            rendered_in=rendered["renderedIn"],
            # The token is deliberately absent from `raw` as well: `raw` reaches the
            # ledger, and a ledger row is the last place a credential belongs.
            raw={"expiresIn": EPHEMERAL_TOKEN_TTL_SECONDS},
        )

    # ------------------------------------------------------------ webhooks ----

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return verify(
            self._simulator.public_key,
            headers=headers,
            body=body,
            now=self._clock(),
            tolerance_seconds=self._tolerance_seconds,
        )

    def webhook_event_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        """A digest of the body, because their envelope has no event id.

        `{eventType, data}` is the whole envelope, so there is nothing provider-
        assigned to key on. Returning the receiver's own fallback explicitly, rather
        than `None`, keeps the ledger's `event_id` and the Redis dedup key provably
        the same value.

        The body alone, deliberately: their retries are re-signed with a fresh
        timestamp, so including the timestamp would make every retry look new. The
        cost is that two genuinely distinct events with identical bytes collapse
        into one — at-most-once, which for a status change is the safer failure.
        A partner integration would ask them for a real event id.
        """
        return hashlib.sha256(body).hexdigest()

    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        envelope = _read_envelope(body)
        occurred_at = self._occurred_at(headers)
        data = envelope.data
        nested = data.get("event")
        transaction: Mapping[str, Any] = nested if isinstance(nested, dict) else {}

        event_type = self._normalized_type(envelope.event_type, transaction)
        currency = _currency_from(transaction)

        return CardEvent(
            provider_id=self.provider_id,
            # Derived, not provider-assigned — see `webhook_event_id`.
            event_id=hashlib.sha256(body).hexdigest(),
            event_type=event_type,
            occurred_at=occurred_at,
            provider_event_type=envelope.event_type,
            card_id=self._resolve_card_id(data, transaction),
            cardholder_id=_optional_str(data.get("userId")),
            amount=_amount_from(transaction, currency),
            # Their transactions carry no reference to our funding intent: money
            # arrives on-chain, so a settlement cannot echo a `funding_ref`.
            # Phase 5 reconciles on the card and the amount instead.
            funding_ref=None,
            card_state=STATUS_NAME_MAP.get(str(data.get("newStatus", ""))),
            challenge_id=_optional_str(data.get("challengeId")),
            challenge_expires_at=_optional_utc(data.get("expiresAt")),
            otp_code=_optional_str(data.get("otpCode")),
            # Untouched, so normalizing never loses anything (SPEC.md §7 stores it)
            # — with one exception, and `_redact_otp` says why.
            raw=_redact_otp(envelope.raw),
        )

    def _occurred_at(self, headers: Mapping[str, str]) -> datetime:
        """When the provider says this happened.

        Only the header knows. Their body carries no timestamp at all, which is the
        second provider to need `parse_webhook`'s headers (ARCHITECTURE.md §4.1).
        """
        timestamp = header_value(headers, TIMESTAMP_HEADER)
        if timestamp is None:
            raise WebhookParseError(f"delivery has no {TIMESTAMP_HEADER} header to date it by")
        try:
            return datetime.fromtimestamp(int(timestamp), tz=UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise WebhookParseError(
                f"delivery {TIMESTAMP_HEADER} is not unix seconds: {exc}"
            ) from exc

    def _normalized_type(self, provider_type: str, transaction: Mapping[str, Any]) -> CardEventType:
        if provider_type == PROVIDER_EVENT_TYPES["transaction_cleared"]:
            kind = str(transaction.get("kind", ""))
            return CLEARED_KIND_MAP.get(kind, CardEventType.UNMAPPED)
        return EVENT_TYPE_MAP.get(provider_type, CardEventType.UNMAPPED)

    def _resolve_card_id(
        self, data: Mapping[str, Any], transaction: Mapping[str, Any]
    ) -> str | None:
        """`cardToken` -> `cardId`.

        Their webhooks name the card by its token and every REST path names it by
        its id, so the adapter has to ask the provider which is which. Unresolvable
        tokens leave `card_id` empty rather than guessing — the token itself is
        still in `raw`.
        """
        token = _optional_str(transaction.get("cardToken")) or _optional_str(data.get("cardToken"))
        if token is None:
            return None
        return self._simulator.card_id_for_token(token)


@dataclass(frozen=True, slots=True)
class _Envelope:
    event_type: str
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

    event_type = payload.get("eventType")
    if not isinstance(event_type, str) or not event_type:
        raise WebhookParseError("delivery envelope is missing a string `eventType`")

    data = payload.get("data")
    return _Envelope(
        event_type=event_type,
        data=data if isinstance(data, dict) else {},
        raw=payload,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_utc(value: object) -> datetime | None:
    """An ISO-8601 timestamp from the payload, or nothing.

    Their bodies carry no timestamps at all as a rule (`_occurred_at` reads the
    header for that reason); the challenge extension is the exception, because a
    challenge has a deadline and only the provider knows it.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _redact_otp(raw: dict[str, Any]) -> dict[str, Any]:
    """The payload, with the one-time code taken out of it.

    The single place where `raw` is not the delivery verbatim, and the reason is
    that `raw` is what the ledger stores (SPEC.md §7) and the ledger is
    append-only: a code written there cannot be removed afterwards, and a
    challenge code with a five-minute life has no business in a permanent audit
    record. What is kept is that a code *was* present — which is the auditable
    fact — under `CardEvent.otp_code`, which never serializes anywhere.

    There is precedent for touching `raw`: the Stripe adapter collapses expanded
    objects back to their ids (docs/ARCHITECTURE.md §8.5). Both are the same
    judgement — `raw` is for reconstructing what a provider said, not for keeping
    everything it happened to include.

    A copy, not a mutation: `_read_envelope` hands out the parsed body and its
    `data` as the same objects, so redacting in place would blank the code before
    the caller has read it.
    """
    data = raw.get("data")
    if not isinstance(data, dict) or "otpCode" not in data:
        return raw
    return {**raw, "data": {**data, "otpCode": REDACTED}}


def _currency_from(transaction: Mapping[str, Any]) -> SafeCurrency | None:
    """Read the token's currency, including its `decimals`, from the payload.

    Falls back to the known Safe currencies by ISO code, so a payload that names
    `code` without `decimals` is still readable.
    """
    kind = str(transaction.get("kind", ""))
    named = transaction.get(f"{kind.lower()}Currency") or transaction.get("billingCurrency")
    if not isinstance(named, dict):
        return None
    symbol = named.get("symbol")
    if isinstance(symbol, str) and symbol in SAFE_CURRENCIES:
        return SAFE_CURRENCIES[symbol]
    code, decimals = named.get("code"), named.get("decimals")
    if not isinstance(code, str) or not isinstance(decimals, int):
        return None
    return SafeCurrency(symbol=str(symbol or code), code=code, decimals=decimals, name=code)


def _amount_from(transaction: Mapping[str, Any], currency: SafeCurrency | None) -> Money | None:
    """Read an amount, or nothing. Absent is not zero.

    Amounts are BigInt strings in token units, so this is where the provider's
    18- or 6-decimal count becomes minor units. `amount` is a **magnitude**: the
    event type carries the direction (ARCHITECTURE.md §4.7).
    """
    if currency is None:
        return None
    kind = str(transaction.get("kind", ""))
    raw_amount = transaction.get(AMOUNT_FIELD_BY_KIND.get(kind, "billingAmount"))
    if raw_amount is None:
        return None
    if not isinstance(raw_amount, str):
        raise WebhookParseError(
            f"amount must be a BigInt string of token units, got {type(raw_amount).__name__}"
        )
    try:
        units = int(raw_amount)
    except ValueError as exc:
        raise WebhookParseError(f"amount is not an integer of token units: {raw_amount!r}") from exc
    try:
        money = to_money(abs(units), currency)
    except ValueError as exc:
        raise WebhookParseError(str(exc)) from exc
    return money
