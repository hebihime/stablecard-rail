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

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.money import Money
from app.issuers.base import (
    Card,
    CardEvent,
    Cardholder,
    CardIssuerAdapter,
    CardNotFoundError,
    CardState,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    FundingResult,
    IllegalCardTransitionError,
    IssuerError,
)
from app.issuers.lithic.client import LithicApiError, LithicClient
from app.issuers.lithic.signing import signing_key

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

__all__ = ["CARD_STATES", "PROVIDER_ID", "LithicAdapter"]


class LithicAdapter(CardIssuerAdapter):
    provider_id = PROVIDER_ID
    funding_model = FundingModel.FIAT_RAIL

    def __init__(
        self,
        *,
        client: LithicClient,
        webhook_secret: str = "",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._secret = webhook_secret
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

    # ------------------------------------------------- funding (phase 3d) ----

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        raise NotImplementedError

    async def get_balance(self, card_id: str) -> Money:
        raise NotImplementedError

    # ------------------------------------------------ webhooks (phase 3e) ----

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        raise NotImplementedError

    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        raise NotImplementedError


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
