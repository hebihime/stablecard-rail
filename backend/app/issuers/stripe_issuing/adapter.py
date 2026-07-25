"""The Stripe Issuing adapter (SPEC.md §3.2, §12.4) — translation, in both directions.

Phase 4 exists to *test* the abstraction rather than extend it: if a second real
provider needed `base.py` to change, that would be a design bug (SPEC.md §12.4).
So this module is where every awkward thing about Stripe stops. `client.py`
speaks HTTP, `signing.py` verifies deliveries, and nothing above
`issuers/base.py` learns that Stripe calls a freeze `inactive` or wants its money
amounts in a form body.

Five translations are worth knowing about before reading the code.

**`inactive` means two different things.** Stripe's card statuses are `active`,
`inactive` and `canceled`. `CardState` distinguishes a card that has never been
activated from one that was and is now blocked, and Stripe does not — while
creating cards `inactive` by default, so both readings are live at once. Mapping
`inactive` to `FROZEN` would report a brand-new card as blocked; mapping it to
`UNACTIVATED` would report a frozen card as never used. So the adapter keeps the
missing bit itself, in the card's own `metadata`: `ACTIVATED_AT_KEY` present means
this card has been activated at least once, therefore `inactive` now means frozen.
That is the same move Lithic makes with its funding tag (docs/ARCHITECTURE.md
§4.5) — the provider's own storage, so it survives our crash — and it is our data
in the sense that Stripe never had it.

**A cardholder needs more identity than the DTO carries.** Stripe requires a
billing address and a display name of at most 24 characters with no digits.
`CreateCardholderRequest` has an email and two names, and inventing fields on it
for one provider is exactly what `raw` and in-adapter placeholders exist to avoid.

**Funding is a spending limit, again.** Stripe cards spend from the *account's*
Issuing balance; there is no per-card balance to move money into. The one per-card
dial is `spending_controls.spending_limits`, and `all_time` is the only interval
that behaves like a balance. Lithic landed in the same place for a different
reason, and the two differ in one useful way: Stripe expresses "unlimited" as the
*absence* of a limit, so a zero limit is a real "cannot spend yet" rather than
Lithic's footgun where `0` means unlimited (docs/ARCHITECTURE.md §8.4).

**Amounts are already integer minor units**, and timestamps are Unix integers —
one conversion fewer than Lithic, and no naive-datetime trap.

**`raw` is an allowlist, not a copy.** Stripe's card object embeds the whole
cardholder, name and postal address included, and can carry `number` and `cvc`.
`raw` reaches the ledger's payload column, so what goes in is named one field at
a time.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.money import Money
from app.issuers.base import (
    Card,
    CardEvent,
    Cardholder,
    CardholderNotFoundError,
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
)
from app.issuers.stripe_issuing.client import StripeApiError, StripeClient
from app.issuers.stripe_issuing.config import get_stripe_issuing_settings
from app.issuers.stripe_issuing.signing import DEFAULT_TOLERANCE_SECONDS

PROVIDER_ID = "stripe_issuing"

#: Provider card status -> ours, before the activation marker is consulted.
#: `inactive` is ambiguous on its own: see `_card_state`. Anything absent is
#: refused rather than guessed at, because mislabelling a state is a claim about
#: whether a card can spend money.
CARD_STATUSES: Mapping[str, CardState] = {
    "active": CardState.ACTIVE,
    "inactive": CardState.UNACTIVATED,
    "canceled": CardState.CANCELED,
}

#: Ours -> theirs, for the three lifecycle calls. `canceled` is irreversible.
TARGET_STATUSES: Mapping[CardState, str] = {
    CardState.ACTIVE: "active",
    CardState.FROZEN: "inactive",
    CardState.CANCELED: "canceled",
}

#: The only spending-limit interval that behaves like a balance. `daily`,
#: `monthly` and the rest reset, and funding would leak away with them.
ALL_TIME = "all_time"

#: Stripe cards are virtual here; physical fulfilment is out of scope (SPEC.md §2).
CARD_TYPE = "virtual"

#: Stripe's cardholder type for a natural person.
CARDHOLDER_TYPE = "individual"

#: Everything this adapter writes into a provider metadata map is prefixed, so it
#: can restate its own keys without touching anybody else's.
METADATA_NAMESPACE = "stablecard_"

#: Records that a card has been activated at least once — the one bit of card state
#: Stripe does not keep and `inactive` is ambiguous without.
ACTIVATED_AT_KEY = f"{METADATA_NAMESPACE}activated_at"

#: Our own cardholder reference, echoed back for reconciliation.
EXTERNAL_REF_KEY = f"{METADATA_NAMESPACE}external_ref"

#: `CreateCardRequest.memo` has nowhere else to go: Stripe's `second_line` is for
#: physical cards only.
MEMO_KEY = f"{METADATA_NAMESPACE}memo"

#: The last funding applied to this card, held by the provider so that it survives
#: our crashing between the call and our own commit.
FUNDING_REF_KEY = f"{METADATA_NAMESPACE}funding_ref"
FUNDING_AMOUNT_KEY = f"{METADATA_NAMESPACE}funding_amount"

#: Stripe caps the cardholder display name and documents "no special characters
#: or numbers" for it.
NAME_MAX_LENGTH = 24

#: Kept: letters, spaces, and the hyphen and apostrophe that appear in real names.
#: Digits and symbols are dropped rather than 400'd, because a caller cannot act on
#: "Stripe rejected your surname".
_NAME_DISALLOWED = re.compile(r"[^A-Za-z \-']")

#: Stripe requires a billing address that SPEC.md §3.1's `CreateCardholderRequest`
#: does not carry. These are obvious placeholders for a test-mode account; a real
#: program submits real identity data through a KYC workflow, which is out of scope
#: (SPEC.md §2). Lithic's adapter carries the same pair for the same reason.
SANDBOX_PHONE_NUMBER = "+15555550123"
SANDBOX_ADDRESS: Mapping[str, str] = {
    "line1": "1 Analytical Engine Way",
    "city": "New York",
    "state": "NY",
    "postal_code": "10128",
    "country": "US",
}

#: Namespaces for the deterministic `Idempotency-Key`s. Two of them, so a card
#: creation and a funding can never derive the same key.
_CREATE_CARD_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stablecard:stripe-issuing:create-card")
_FUND_CARD_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stablecard:stripe-issuing:fund-card")

__all__ = [
    "ACTIVATED_AT_KEY",
    "ALL_TIME",
    "CARD_STATUSES",
    "FUNDING_AMOUNT_KEY",
    "FUNDING_REF_KEY",
    "PROVIDER_ID",
    "SANDBOX_ADDRESS",
    "StripeIssuingAdapter",
]


class StripeIssuingAdapter(CardIssuerAdapter):
    provider_id = PROVIDER_ID
    funding_model = FundingModel.FIAT_RAIL

    def __init__(
        self,
        *,
        client: StripeClient,
        webhook_secret: str = "",
        signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._secret = webhook_secret
        self._tolerance_seconds = signature_tolerance_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(cls) -> StripeIssuingAdapter:
        """The factory the registry holds (see `app/issuers/__init__.py`).

        Nothing here validates a credential. `registry.describe()` builds every
        registered adapter to report its funding model and `GET /providers` calls
        it, so an adapter that refused to *exist* without a key would take that
        route down for the providers that are configured. The key is checked on the
        request path instead, where the message can name the variable.
        """
        settings = get_stripe_issuing_settings()
        return cls(
            client=StripeClient(
                base_url=settings.api_base_url,
                api_key=settings.api_key,
                timeout=settings.request_timeout_seconds,
                api_version=settings.api_version,
            ),
            webhook_secret=settings.webhook_secret,
            signature_tolerance_seconds=settings.signature_tolerance_seconds,
        )

    # --------------------------------------------------------- cardholders ----

    async def create_cardholder(self, req: CreateCardholderRequest) -> Cardholder:
        payload: dict[str, Any] = {
            "type": CARDHOLDER_TYPE,
            "name": _display_name(req.first_name, req.last_name),
            "email": req.email,
            "phone_number": SANDBOX_PHONE_NUMBER,
            "billing": {"address": dict(SANDBOX_ADDRESS)},
            # Stripe lists both as `past_due` until they are set, and a card cannot
            # be activated while a requirement is outstanding.
            "individual": {"first_name": req.first_name, "last_name": req.last_name},
        }
        if req.external_ref:
            payload["metadata"] = {EXTERNAL_REF_KEY: req.external_ref}

        holder = await self._client.post("/issuing/cardholders", body=payload)
        return Cardholder(
            provider_id=self.provider_id,
            cardholder_id=_require_str(holder, "id"),
            # Theirs if they echoed it, ours otherwise. Either way it is the same
            # address; reading it back proves the round trip.
            email=_optional_str(holder.get("email")) or req.email,
            state=_optional_str(holder.get("status")) or "active",
            created_at=_utc(holder.get("created"), fallback=self._clock()),
            # Named one at a time: the response also carries the name, the phone
            # number and the postal address, and `raw` reaches the ledger.
            raw={
                "cardholder_type": holder.get("type"),
                # Why a card of theirs might refuse to activate. The entries are
                # field *paths*, not values.
                "requirements": holder.get("requirements"),
                "metadata": _our_metadata(holder),
            },
        )

    # --------------------------------------------------------------- cards ----

    async def create_card(self, cardholder_id: str, req: CreateCardRequest) -> Card:
        currency = req.currency.lower()
        payload: dict[str, Any] = {
            "cardholder": cardholder_id,
            "currency": currency,
            "type": CARD_TYPE,
            # Stripe's own default, stated rather than assumed: the state this
            # returns should not be a guess about what they did.
            "status": "inactive",
        }
        if req.spend_limit_minor is not None:
            payload["spending_controls"] = {
                "spending_limits": [
                    {"amount": _checked_limit(req.spend_limit_minor), "interval": ALL_TIME}
                ],
                "spending_limits_currency": currency,
            }
        if req.memo:
            payload["metadata"] = {MEMO_KEY: req.memo}

        try:
            card = await self._client.post(
                "/issuing/cards",
                body=payload,
                idempotency_key=_create_card_key(cardholder_id, req),
            )
        except StripeApiError as exc:
            if _is_missing(exc):
                raise CardholderNotFoundError(cardholder_id) from exc
            raise
        return self._to_card(card)

    async def get_card(self, card_id: str) -> Card:
        return self._to_card(await self._read_card(card_id))

    async def activate_card(self, card_id: str) -> Card:
        """Activate — also the unfreeze path, per SPEC.md §9.1's toggle.

        Reads the card first, in order to restate the metadata keys this adapter
        owns alongside the new activation marker. Stripe documents metadata as
        merged key-by-key, in which case restating is a no-op; if it ever replaced
        the map, an unfreeze would erase the funding idempotency record. One read
        is cheap insurance against an assumption this suite cannot verify
        (docs/ARCHITECTURE.md §8.5).
        """
        current = await self._read_card(card_id)
        marked = {**_our_metadata(current), ACTIVATED_AT_KEY: self._clock().isoformat()}
        return await self._transition(card_id, CardState.ACTIVE, metadata=marked)

    async def freeze_card(self, card_id: str) -> Card:
        # No metadata in the body at all, so there is nothing for Stripe to merge
        # or replace and the activation marker cannot be disturbed.
        return await self._transition(card_id, CardState.FROZEN)

    async def cancel_card(self, card_id: str) -> Card:
        return await self._transition(card_id, CardState.CANCELED)

    async def _transition(
        self, card_id: str, target: CardState, *, metadata: Mapping[str, str] | None = None
    ) -> Card:
        body: dict[str, Any] = {"status": TARGET_STATUSES[target]}
        if metadata:
            body["metadata"] = dict(metadata)
        try:
            # Stripe has no PATCH: an update is a POST to the object's own path.
            updated = await self._client.post(f"/issuing/cards/{card_id}", body=body)
        except StripeApiError as exc:
            if exc.status == 400 and not _is_missing(exc):
                # A caller mistake (409) rather than a provider failure (502) — but
                # only if the card really is somewhere the transition cannot start
                # from, which costs one extra read to establish.
                raise await self._illegal_transition(card_id, target, exc) from exc
            raise self._translate(card_id, exc) from exc
        return self._to_card(updated)

    async def _illegal_transition(
        self, card_id: str, target: CardState, original: StripeApiError
    ) -> IssuerError:
        """Stripe's refusal, re-read as a state question.

        Deliberately keyed on the card's state rather than on their message or
        error code: the exact wording for "you cannot update a canceled card" is
        not something these fixtures can vouch for, and a state read back is.
        """
        try:
            current = await self.get_card(card_id)
        except IssuerError:
            # No idea what state it is in, so do not invent one.
            return original
        if current.state is CardState.CANCELED and target is not CardState.CANCELED:
            return IllegalCardTransitionError(card_id, current.state, target)
        # They refused for a reason we cannot name, so theirs stands.
        return original

    async def _read_card(self, card_id: str) -> dict[str, Any]:
        try:
            return await self._client.get(f"/issuing/cards/{card_id}")
        except StripeApiError as exc:
            raise self._translate(card_id, exc) from exc

    @staticmethod
    def _translate(card_id: str, exc: StripeApiError) -> IssuerError:
        """Provider failure -> the vocabulary in `issuers/base.py`."""
        if _is_missing(exc):
            return CardNotFoundError(card_id)
        return exc

    def _to_card(self, card: Mapping[str, Any]) -> Card:
        status = _require_str(card, "status")
        if status not in CARD_STATUSES:
            raise IssuerError(
                f"stripe reported card status {status!r}, which this adapter does not map. "
                f"Calling it something else would be a claim about whether the card can spend."
            )
        metadata = _our_metadata(card)
        holder = _reference(card.get("cardholder"))
        if not holder:
            raise IssuerError("stripe card response has no cardholder to attribute it to")
        limit = _all_time_limit(card)
        return Card(
            provider_id=self.provider_id,
            card_id=_require_str(card, "id"),
            cardholder_id=holder,
            state=_card_state(status, metadata),
            last_four=_require_str(card, "last4"),
            exp_month=_as_int(card.get("exp_month"), "exp_month"),
            exp_year=_as_int(card.get("exp_year"), "exp_year"),
            # Stripe's currency codes are lower-case; `Money`'s are not.
            currency=str(card.get("currency") or "usd").upper(),
            spend_limit_minor=limit,
            # A fiat rail has no deposit address to hand out (SPEC.md §3.2).
            deposit_address=None,
            created_at=_utc(card.get("created"), fallback=self._clock()),
            # Named one at a time. The response embeds the whole cardholder — name,
            # email, postal address — and can carry `number` and `cvc`.
            raw={
                "brand": card.get("brand"),
                "provider_status": status,
                "card_type": card.get("type"),
                "spending_limit_interval": ALL_TIME if limit is not None else None,
                "cancellation_reason": card.get("cancellation_reason"),
                "replacement_for": _reference(card.get("replacement_for")),
                "livemode": card.get("livemode"),
                "metadata": metadata,
            },
        )

    # --------------------------------------------------------------- money ----

    async def fund_card(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        """Fund by raising the card's `all_time` spending limit.

        Stripe cards spend from the *account's* Issuing balance; there is no
        per-card balance to move money into, so the one per-card dial is the
        spending limit — and `all_time` is the only interval that behaves like a
        balance rather than an allowance that resets.

        Idempotency comes from two places, deliberately. `Idempotency-Key` covers a
        retry inside Stripe's 24-hour window and replays *their* record, which is
        the stronger guarantee and one Lithic could not offer (their key works on
        creation only). The metadata marker covers a retry after that window, and
        survives our crashing mid-call because the provider holds it. What neither
        can do is remember an older ref once a newer funding has replaced the
        marker — the same limitation Lithic's memo tag has, kept identical on
        purpose (docs/ARCHITECTURE.md §8.4).
        """
        payload = await self._read_card(card_id)
        current = self._to_card(payload)
        metadata = _our_metadata(payload)

        if metadata.get(FUNDING_REF_KEY) == funding_ref:
            applied = _applied_amount(card_id, funding_ref, metadata)
            if applied != amount.amount_minor:
                raise FundingRejectedError(
                    card_id,
                    f"funding ref {funding_ref!r} was already applied for "
                    f"{applied} minor units, not {amount.amount_minor}",
                )
            return self._funding_result(
                card_id, amount, funding_ref, current.spend_limit_minor or 0, replayed=True
            )

        if amount.amount_minor <= 0:
            raise FundingRejectedError(card_id, f"amount must be positive, got {amount}")
        if amount.currency != current.currency:
            raise FundingRejectedError(
                card_id, f"card is denominated in {current.currency}, not {amount.currency}"
            )
        if current.state is CardState.CANCELED:
            raise FundingRejectedError(card_id, "card is canceled")
        if current.spend_limit_minor is None:
            raise FundingRejectedError(
                card_id,
                f"card has no {ALL_TIME} spending limit, which Stripe reads as unlimited; "
                f"raising it would replace an unlimited card with a limited one. A limit on "
                f"another interval does not count: it resets, so funding would leak away.",
            )

        raised = current.spend_limit_minor + amount.amount_minor
        try:
            await self._client.post(
                f"/issuing/cards/{card_id}",
                body={
                    "spending_controls": {
                        # Restated every time: a limit on any other interval is not a
                        # balance, and this write replaces the whole control.
                        "spending_limits": [{"amount": raised, "interval": ALL_TIME}],
                        "spending_limits_currency": current.currency.lower(),
                    },
                    # Our own keys restated alongside the new marker, for the reason
                    # in `activate_card`.
                    "metadata": {
                        **metadata,
                        FUNDING_REF_KEY: funding_ref,
                        FUNDING_AMOUNT_KEY: str(amount.amount_minor),
                    },
                },
                idempotency_key=_funding_key(card_id, funding_ref, amount),
            )
        except StripeApiError as exc:
            raise self._translate(card_id, exc) from exc
        return self._funding_result(
            card_id, amount, funding_ref, raised, before=current.spend_limit_minor
        )

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
                "spending_limit_before": before if before is not None else after,
                "spending_limit_after": after,
                "replayed": replayed,
            },
        )

    async def get_balance(self, card_id: str) -> Money:
        """Available spend: the card's limit, less what it has spent and is holding.

        Stripe exposes no per-card balance — `GET /v1/balance` reports the whole
        account's Issuing funds — so this is derived, exactly as Lithic's is. The
        numbers come from two endpoints rather than one: a settled purchase is a
        negative `issuing.transaction` and a refund a positive one, so they sum
        straight in, while an approved authorization still `pending` is a positive
        hold and comes off. Nothing is counted twice, because an authorization
        leaves `pending` precisely when it becomes a transaction or releases.

        Both lists are read to the end, or a busy card reports a balance that is
        too high.
        """
        card = self._to_card(await self._read_card(card_id))
        if card.spend_limit_minor is None:
            raise IssuerError(
                f"card {card_id} has no {ALL_TIME} spending limit, which Stripe reads as "
                f"unlimited; there is no available balance to report"
            )
        settled = await self._client.list_all("/issuing/transactions", params={"card": card_id})
        holds = await self._client.list_all(
            # Filtered provider-side: a closed authorization has either become a
            # transaction or released its hold, so asking for one would be asking to
            # double-count it.
            "/issuing/authorizations",
            params={"card": card_id, "status": "pending"},
        )
        return Money(
            card.spend_limit_minor
            + sum(_transaction_impact(entry) for entry in settled)
            - sum(_authorization_hold(entry) for entry in holds),
            card.currency,
        )

    # ------------------------------------------------------------ webhooks ----

    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        raise NotImplementedError("phase 4e")

    async def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> CardEvent:
        raise NotImplementedError("phase 4e")


# ------------------------------------------------------------------- state ----


def _card_state(status: str, metadata: Mapping[str, str]) -> CardState:
    """Stripe's status, plus the bit they do not keep.

    `inactive` is where a card that has never been activated and a card that was
    frozen land at the same place. The marker is what tells them apart.
    """
    state = CARD_STATUSES[status]
    if state is CardState.UNACTIVATED and metadata.get(ACTIVATED_AT_KEY):
        return CardState.FROZEN
    return state


def _our_metadata(payload: Mapping[str, Any]) -> dict[str, str]:
    """Only the metadata keys this adapter wrote.

    Restating a key someone else set would be rewriting data that is not ours,
    and under Stripe's documented merge semantics it is untouched either way.
    """
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in metadata.items()
        if str(key).startswith(METADATA_NAMESPACE) and value is not None
    }


def _all_time_limit(card: Mapping[str, Any]) -> int | None:
    """The card's `all_time` spending limit, or `None` for no limit at all.

    `None` means *unlimited*, which at Stripe is said by the absence of a limit
    rather than by a zero — so a zero here is a real "cannot spend yet". A limit on
    any other interval is deliberately not reported: it resets, so it is not a
    balance, and `fund_card` must refuse to treat it as one.
    """
    controls = card.get("spending_controls")
    if not isinstance(controls, Mapping):
        return None
    limits = controls.get("spending_limits")
    if not isinstance(limits, list):
        return None
    for entry in limits:
        if not isinstance(entry, Mapping) or entry.get("interval") != ALL_TIME:
            continue
        amount = entry.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise IssuerError(
                f"stripe reported an {ALL_TIME} spending limit that is not integer minor "
                f"units: {amount!r}"
            )
        return amount
    return None


# ------------------------------------------------------------------ errors ----


def _is_missing(exc: StripeApiError) -> bool:
    """Whether a failure means "no such object".

    Stripe answers 404 for an id that does not exist and 400 with
    `code: resource_missing` for one that cannot refer to anything. Provider ids
    are opaque to us (SPEC.md §1), so we cannot check a shape ourselves — and "not
    an id of theirs" and "no such object" are the same fact whichever status they
    choose to say it with.
    """
    return exc.status == 404 or exc.code == "resource_missing"


# ----------------------------------------------------------------- reading ----


def _display_name(first_name: str, last_name: str) -> str:
    """A cardholder name Stripe will accept.

    They cap it at 24 characters and document "no special characters or numbers".
    Cleaning it here beats a 400 the caller cannot act on — but a name with no
    letters in it at all is a caller mistake worth naming.
    """
    collapsed = " ".join(_NAME_DISALLOWED.sub("", f"{first_name} {last_name}").split())
    if not collapsed:
        raise IssuerError(
            f"stripe requires a cardholder name of letters only, and "
            f"{first_name + ' ' + last_name!r} has none"
        )
    return collapsed[:NAME_MAX_LENGTH].strip()


def _create_card_key(cardholder_id: str, req: CreateCardRequest) -> str:
    """A deterministic `Idempotency-Key` for one logical card creation.

    Deriving the key from the request means a retry reuses it without the caller
    having to thread one through — at the cost that asking twice for a
    byte-identical card yields one card. Vary the memo to mean it.

    Unlike Lithic, Stripe honours the header on every POST, and keeps keys for at
    least 24 hours. A retry after that window would be a fresh request, which is
    why `fund_card` does not rely on this alone (4d).
    """
    material = f"{cardholder_id}|{req.currency}|{req.spend_limit_minor}|{req.memo}"
    return str(uuid.uuid5(_CREATE_CARD_NAMESPACE, material))


def _funding_key(card_id: str, funding_ref: str, amount: Money) -> str:
    """A deterministic `Idempotency-Key` for one logical funding.

    The amount is part of the material on purpose: two calls with the same ref and
    different amounts must not be quietly collapsed into one by Stripe's
    idempotency layer, they must reach the check in `fund_card` and be refused.
    """
    material = f"{card_id}|{funding_ref}|{amount.amount_minor}|{amount.currency}"
    return str(uuid.uuid5(_FUND_CARD_NAMESPACE, material))


def _applied_amount(card_id: str, funding_ref: str, metadata: Mapping[str, str]) -> int:
    """What this ref was recorded as funding, from the card's own metadata.

    An unreadable amount is refused rather than guessed at: funding again might
    double it and assuming it landed might skip it, so the only answer that cannot
    lose money silently is to stop and say the record is broken.
    """
    recorded = metadata.get(FUNDING_AMOUNT_KEY, "")
    try:
        return int(recorded)
    except ValueError as exc:
        raise FundingRejectedError(
            card_id,
            f"funding ref {funding_ref!r} is recorded on the card with an unreadable "
            f"amount {recorded!r}, so whether it was applied cannot be established",
        ) from exc


def _transaction_impact(transaction: Mapping[str, Any]) -> int:
    """What one settled transaction does to available spend, in minor units.

    Stripe signs these for us — a capture is negative and a refund positive — so
    there is no status table to get wrong. Reading one wrong must fail loudly
    rather than contribute nothing, because a plausible wrong balance funds the
    wrong amount.
    """
    amount = transaction.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise IssuerError(
            f"stripe transaction {transaction.get('id')!r} has a non-integer amount: {amount!r}"
        )
    return amount


def _authorization_hold(authorization: Mapping[str, Any]) -> int:
    """What one pending authorization is holding, in minor units.

    Only an *approved* one holds anything: declined money is not held money, and
    treating it as held would understate the balance and refuse a top-up the card
    could take.
    """
    if authorization.get("approved") is not True:
        return 0
    amount = authorization.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise IssuerError(
            f"stripe authorization {authorization.get('id')!r} has a non-integer amount: {amount!r}"
        )
    return amount


def _checked_limit(spend_limit_minor: int) -> int:
    if spend_limit_minor < 0:
        raise IssuerError(
            f"a spending limit cannot be negative, got {spend_limit_minor}; omit the "
            f"limit to mean unlimited, which is how Stripe says it"
        )
    return spend_limit_minor


def _reference(value: object) -> str | None:
    """An id from a field Stripe sends either expanded or as a bare string.

    The card object embeds the cardholder; transactions and authorizations name it.
    Both have to resolve to the same opaque id.
    """
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        return _optional_str(value.get("id"))
    return None


def _require_str(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise IssuerError(f"stripe response is missing a string {field!r}")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int(value: object, field: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise IssuerError(f"stripe {field} is not a number: {value!r}") from exc


def _utc(value: object, *, fallback: datetime) -> datetime:
    """An aware UTC timestamp from a Stripe Unix timestamp.

    Integers throughout, so the naive-ISO-string trap that phase 3 hit with
    Lithic's account holders cannot recur here.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return fallback
