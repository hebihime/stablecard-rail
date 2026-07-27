"""The fake provider behind the Gnosis Pay mock: their API, plus the chain.

For Lithic and Stripe everything on the far side of the adapter belongs to someone
else. For `gnosis_pay_mock` it lives here — and for this provider "the far side"
is two things, which is the whole point:

* **Their API.** Users, a Safe per user, virtual cards, the card lifecycle,
  transactions, and an outbox of **signed webhook deliveries** identical in shape
  to what would arrive over HTTP. Methods are named after the endpoints they stand
  for, so the adapter reads like the integration it is a rehearsal for.
* **The chain.** `receive_onchain_deposit` is a stablecoin transfer landing in the
  Safe. It is not an API call and there is no API call that could replace it: for a
  `CRYPTO_DEPOSIT` provider, money arrives on-chain and the provider merely
  observes it. That is why `confirm_funding` verifies and attributes rather than
  credits (SPEC.md §3.2).

Shaped on https://docs.gnosispay.com, read 2026-07-25. Where the docs stop short
the gap is marked in a comment rather than guessed at quietly.

Everything is deterministic. Identifiers come from per-kind counters and the Safe
address is derived by hash, so a demo run twice produces the same ids and a test
never needs to know a random value.

State is per instance, which is why the registry keeps one adapter per process.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.money import Money
from app.issuers.base import (
    CardholderNotFoundError,
    CardNotFoundError,
    CardState,
    ChallengeAlreadyAnsweredError,
    ChallengeNotFoundError,
    FundingRejectedError,
    FundingResult,
    FundingStatus,
    IllegalCardTransitionError,
    IssuerError,
)
from app.issuers.gnosis_pay_mock.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    derive_signing_key,
    public_key_document,
    sign,
)

PROVIDER_ID = "gnosis_pay_mock"

#: The surfaces this stands in for. Recorded so the mock cannot drift away from
#: the integration it rehearses without the diff showing it.
API_BASE_URL = "https://api.gnosispay.com/api/v1"
PSE_BASE_URL = "https://api-pse.gnosispay.com/api/v1"

#: Gnosis Pay Safes are deployed on Gnosis Chain, which is also where SPEC.md
#: §5.2's bridge lands.
CHAIN_NAME = "gnosis"

#: "Users may maintain a maximum of 5 active cards including physical and virtual
#: cards combined" — `POST /api/v1/cards/virtual` answers 422 past it.
MAX_ACTIVE_CARDS = 5

#: `POST /api/v1/ephemeral-token` mints a token with a 60-second lifespan.
EPHEMERAL_TOKEN_TTL_SECONDS = 60

#: How long an emitted 3DS challenge stays answerable. Five minutes, matching the
#: `start_time`/`expiry_time` gap in Lithic's own challenge fixture — a real 3DS
#: challenge is minutes, not hours, and the OTP service takes its TTL from this
#: rather than from a default of ours (docs/ARCHITECTURE.md §11.3).
CHALLENGE_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class SafeCurrency:
    """What a Safe holds, and how the provider counts it.

    Gnosis Pay reports amounts as BigInt strings in **token** units alongside a
    `decimals` field (`billingCurrency: {symbol, code, decimals, name}`), so the
    count is not our 2-dp minor unit and the adapter has to convert.

    `decimals` here is representative rather than verified against a chain
    explorer: the docs name the field but publish no values. What matters
    architecturally is that it is neither 2 nor the same for every currency, which
    is what stops the conversion from being quietly hard-coded. Token addresses are
    deliberately absent — inventing on-chain identifiers would be worse than
    omitting them.
    """

    symbol: str
    #: ISO-4217, which is what `Money` carries.
    code: str
    decimals: int
    name: str


SAFE_CURRENCIES: Mapping[str, SafeCurrency] = {
    "EURe": SafeCurrency("EURe", "EUR", 18, "Monerium EUR emoney"),
    "GBPe": SafeCurrency("GBPe", "GBP", 18, "Monerium GBP emoney"),
    "USDCe": SafeCurrency("USDCe", "USD", 6, "USD Coin on Gnosis"),
}

#: USDCe, so the demo stays USD-denominated end to end: SPEC.md §5.2 bridges
#: Solana **USDC**, which is also 6 decimals.
DEFAULT_SAFE_CURRENCY = "USDCe"

#: Gnosis Pay publishes the *set* of numeric card status codes and the *set* of
#: status names, but not which pairs with which. Only `1000 = Active` is
#: documented (`GET /api/v1/cards?status_code=1000` "returns only active cards").
#: The rest of this table is the mock's own assignment from their published code
#: set, which is why the adapter derives `CardState` from the boolean flags the
#: status endpoint documents and never from this number.
MOCK_STATUS_CODES: Mapping[str, int] = {
    "Active": 1000,
    "Inactive": 1199,
    "Frozen": 1062,
    "Void": 1008,
    "Lost": 1009,
    "Stolen": 1041,
}

#: Their event vocabulary (docs.gnosispay.com/webhooks/events). Deliberately not
#: our normalized names, so the adapter has real translation to do (SPEC.md §3.3).
#: `card.transaction.cleared` is one label covering three of our types.
PROVIDER_EVENT_TYPES = {
    "card_lifecycle": "card.status.changed",
    "authorization": "card.transaction.created",
    "transaction_cleared": "card.transaction.cleared",
    "user_created": "user.created",
    "kyc_status": "kyc.status.changed",
}

#: Two events our normalized vocabulary requires (SPEC.md §3.3) that Gnosis Pay
#: does not publish. They are extensions, labelled as such: SPEC.md §6 leans on
#: this simulator for the phase-7 OTP path, and §3.3 lists `chargeback`, whose
#: nearest documented surface is `POST /api/v1/transactions/{id}/dispute` — an
#: endpoint with no matching webhook.
EXTENSION_EVENT_TYPES = {
    "three_ds_challenge": "card.three-ds.challenge",
    "chargeback": "card.transaction.disputed",
}

#: ISO 8583 transaction type, as their transaction objects carry it. "00" is a
#: purchase.
PURCHASE_TRANSACTION_TYPE = "00"


def _scale(currency: SafeCurrency) -> int:
    # Annotated because `int ** int` widens to Any: a negative exponent would give
    # a float, which `decimals >= 2` rules out here.
    scale: int = 10 ** (currency.decimals - 2)
    return scale


def to_token_units(amount: Money, currency: SafeCurrency) -> int:
    """Minor units -> the provider's token units."""
    return amount.amount_minor * _scale(currency)


def to_money(units: int, currency: SafeCurrency) -> Money:
    """Token units -> minor units, or refuse.

    An 18-decimal token can express amounts a 2-dp `Money` cannot. Refusing beats
    rounding: silently dropping a fraction of a cent in a funding pipeline is a
    reconciliation incident, and the caller can see the exact value in `raw`.
    """
    minor, remainder = divmod(units, _scale(currency))
    if remainder:
        raise ValueError(
            f"{units} {currency.symbol} units is finer than {currency.code} minor units"
        )
    return Money(minor, currency.code)


@dataclass(frozen=True, slots=True)
class Delivery:
    """A signed webhook delivery, exactly as it would arrive over HTTP."""

    provider_id: str
    #: The provider's own `eventType`, pre-normalization.
    event_type: str
    headers: dict[str, str]
    body: bytes

    @property
    def derived_event_id(self) -> str:
        """The dedup identity of this delivery.

        Derived, not provider-assigned: their envelope carries no event id. See
        `GnosisPayMockAdapter.webhook_event_id`.
        """
        return hashlib.sha256(self.body).hexdigest()


@dataclass(slots=True)
class UserRecord:
    """A Gnosis Pay user and the Safe that *is* their account."""

    user_id: str
    email: str
    first_name: str
    last_name: str
    external_ref: str | None
    safe_address: str
    currency: SafeCurrency
    #: Confirmed, spendable token units sitting in the Safe.
    spendable_units: int
    #: Token units moved to the on-chain hold account by an authorization, which
    #: have left the Safe but not yet reached a merchant.
    held_units: int
    #: The Roles-module daily allowance, in minor units. `None` is unlimited.
    #: Per Safe, therefore shared by every card the user holds.
    daily_limit_minor: int | None
    safe_deployed: bool
    created_at: datetime


@dataclass(slots=True)
class CardRecord:
    card_id: str
    #: A second, distinct identifier. Their card objects carry both `id` and
    #: `cardToken`, and transactions and webhooks reference the *token* while every
    #: REST path takes the `cardId`.
    card_token: str
    user_id: str
    virtual: bool
    last_four: str
    exp_month: int
    exp_year: int
    activated_at: datetime | None
    is_frozen: bool = False
    is_lost: bool = False
    is_stolen: bool = False
    is_void: bool = False
    #: Documented on the status endpoint. Nothing in the mock sets it: their docs
    #: describe a blocked card only as a reason `/activate` answers 422.
    is_blocked: bool = False
    previous_status_name: str = "Inactive"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def status_name(self) -> str:
        if self.is_void:
            return "Void"
        if self.is_lost:
            return "Lost"
        if self.is_stolen:
            return "Stolen"
        if self.is_frozen:
            return "Frozen"
        return "Active" if self.activated_at is not None else "Inactive"

    @property
    def status_code(self) -> int:
        return MOCK_STATUS_CODES[self.status_name]

    @property
    def card_state(self) -> CardState:
        """The provider's status, in our vocabulary.

        Read from the booleans, never from `status_code` — see `MOCK_STATUS_CODES`.
        Void, lost and stolen all collapse to `CANCELED`: each is terminal and each
        requires a replacement card, which is the only distinction our model makes.
        """
        if self.is_void or self.is_lost or self.is_stolen:
            return CardState.CANCELED
        if self.is_frozen:
            return CardState.FROZEN
        return CardState.ACTIVE if self.activated_at is not None else CardState.UNACTIVATED


@dataclass(slots=True)
class SafeDeposit:
    """A stablecoin transfer that landed in a Safe. Created by the chain, not by us."""

    deposit_id: str
    tx_hash: str
    safe_address: str
    amount_units: int
    currency: SafeCurrency
    #: Unconfirmed deposits are visible but not spendable — the provider's
    #: `pending` balance, and SPEC.md §5.2's reason to wait for `finalized`.
    confirmed: bool
    received_at: datetime
    #: Our funding intent, once `confirm_funding` has attributed this deposit.
    funding_ref: str | None = None


@dataclass(slots=True)
class ChallengeRecord:
    """A 3DS challenge this simulator has issued, and what became of it.

    An extension rather than a documented Gnosis Pay object — see
    `EXTENSION_EVENT_TYPES` — modelled on the state a real ACS keeps: a challenge is
    open until it is answered or it expires, and it can be answered exactly once.
    """

    challenge_id: str
    card_id: str
    code: str
    issued_at: datetime
    expires_at: datetime
    #: `None` while open; `"approve"` or `"decline"` once answered.
    answer: str | None = None
    answered_at: datetime | None = None


@dataclass(slots=True)
class TransactionRecord:
    thread_id: str
    card_token: str
    user_id: str
    #: `Payment`, `Refund` or `Reversal`.
    kind: str
    #: `Approved`, `InsufficientFunds`, … `Refund` and `Reversal` carry none.
    status: str | None
    billing_units: int
    currency: SafeCurrency
    merchant_name: str
    merchant_city: str
    merchant_country: str
    mcc: str
    is_pending: bool
    created_at: datetime
    cleared_at: datetime | None = None


def safe_address_for(user_id: str, *, program: str) -> str:
    """Derive the user's Safe address.

    Deterministic and obviously synthetic: the real one is a contract address from
    a Safe deployment. 20 bytes, hex, `0x`-prefixed, so it is the right *shape* for
    the chain code in phase 5 to handle.
    """
    digest = hashlib.sha256(f"{program}:safe:{user_id}".encode()).hexdigest()
    return f"0x{digest[:40]}"


class GnosisPaySimulator:
    """In-process stand-in for Gnosis Pay, and for the chain underneath it.

    Convention for the mutating methods: a call that creates something you may
    need to reference again returns the record (`create_user`, `authorize`); a call
    whose result is a webhook returns the `Delivery` it emitted. Everything emitted
    is also queued on `deliveries`.
    """

    def __init__(
        self,
        *,
        signing_key: Ed25519PrivateKey | None = None,
        clock: Callable[[], datetime] | None = None,
        program: str = "stablecard-demo",
    ) -> None:
        #: The private half of the webhook keypair. Only the provider holds one —
        #: the adapter verifies with the published public half, exactly as a
        #: partner integration would.
        self._signing_key = signing_key or derive_signing_key()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._program = program
        self._counters: dict[str, int] = {}
        self._users: dict[str, UserRecord] = {}
        self._safes: dict[str, str] = {}
        self._cards: dict[str, CardRecord] = {}
        self._tokens: dict[str, str] = {}
        self._deposits: list[SafeDeposit] = []
        self._transactions: dict[str, TransactionRecord] = {}
        #: `funding_ref` -> the result returned the first time. The whole of
        #: `fund_card` idempotency (SPEC.md §10) is this dictionary.
        self._fundings: dict[str, FundingResult] = {}
        #: Challenges this simulator has issued, so it can accept an answer to one.
        #: A real ACS holds the same state; without it `respond_to_challenge` would
        #: have nothing to refuse, and "answer a challenge that does not exist"
        #: would silently succeed.
        self._challenges: dict[str, ChallengeRecord] = {}
        self._ephemeral: dict[str, tuple[str, datetime]] = {}
        self._spent_ephemeral: set[str] = set()
        self._deliveries: list[Delivery] = []

    # ------------------------------------------------------- webhook keys ----

    @property
    def public_key(self) -> Ed25519PublicKey:
        """The verifying key, as a partner would hold it after fetching it once."""
        return self._signing_key.public_key()

    def public_key_endpoint(self) -> dict[str, Any]:
        """`GET https://webhooks.gnosispay.com/api/v1/public-key`.

        Nothing in this repo fetches it — the adapter is handed the key by the
        constructor, because over HTTP this is one cached call at startup and not
        part of what the pipeline is here to exercise. Modelled anyway so the
        published shape is written down where the integration will need it.
        """
        return public_key_document(self.public_key)

    # ------------------------------------------------------------ helpers ----

    def _next(self, kind: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{kind}_{self._counters[kind]:06d}"

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def advance_clock(self, moment: datetime) -> None:
        """Pin the provider's clock. For tests and demos of expiry paths."""
        self._clock = lambda: moment

    def seed_sequence(self, kind: str, value: int) -> None:
        """Start one id counter partway along. For demos against a shared database.

        This simulator is in-process, so every restart numbers its objects from 1
        again — while the ledger is append-only and outlives the run. A demo run
        therefore re-issues `3ds_000001`, whose ledger row already exists under the
        same idempotency key, and the second run is either refused or reports the
        first run's row as its own. Neither is a bug in the service (a provider
        genuinely reusing an id should collapse to one row) but both make a demo
        lie about what it just did.

        A sibling of `advance_clock`, and for the same reason: state a real provider
        keeps and this one has to be told.
        """
        self._counters[kind] = max(self._counters.get(kind, 0), value)

    def _require_user(self, user_id: str) -> UserRecord:
        user = self._users.get(user_id)
        if user is None:
            raise CardholderNotFoundError(user_id)
        return user

    def _require_card(self, card_id: str) -> CardRecord:
        card = self._cards.get(card_id)
        if card is None:
            raise CardNotFoundError(card_id)
        return card

    def _require_transaction(self, thread_id: str) -> TransactionRecord:
        transaction = self._transactions.get(thread_id)
        if transaction is None:
            raise IssuerError(f"no such transaction at this provider: {thread_id!r}")
        return transaction

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(self._cards)

    def card_id_for_token(self, card_token: str) -> str | None:
        """`cardToken` -> `cardId`.

        Their webhooks name the token and their REST paths name the id, so this
        lookup is unavoidable — a real adapter would spend a `GET /api/v1/cards`
        on it, which makes normalizing a delivery fallible for a network reason.
        """
        return self._tokens.get(card_token)

    # -------------------------------------------------- users and the Safe ----

    def create_user(
        self,
        *,
        email: str,
        first_name: str,
        last_name: str,
        external_ref: str | None = None,
        safe_currency: str = DEFAULT_SAFE_CURRENCY,
    ) -> UserRecord:
        """`POST /api/v1/auth/signup` then `POST /api/v1/safe/deploy`.

        Deployment is immediate here. Upstream it is asynchronous, with
        `GET /api/v1/safe/deployment-status` to poll — modelled as a status
        accessor rather than a pending state, because nothing consumes one yet.
        """
        user_id = self._next("usr")
        record = UserRecord(
            user_id=user_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            external_ref=external_ref,
            safe_address=safe_address_for(user_id, program=self._program),
            currency=SAFE_CURRENCIES[safe_currency],
            spendable_units=0,
            held_units=0,
            daily_limit_minor=None,
            safe_deployed=True,
            created_at=self._now(),
        )
        self._users[user_id] = record
        self._safes[record.safe_address] = user_id
        return record

    def get_user(self, user_id: str) -> UserRecord:
        """`GET /api/v1/user`."""
        return self._require_user(user_id)

    def safe_deployment_status(self, user_id: str) -> dict[str, Any]:
        """`GET /api/v1/safe/deployment-status`."""
        user = self._require_user(user_id)
        return {
            "safeAddress": user.safe_address,
            "isDeployed": user.safe_deployed,
            "chain": CHAIN_NAME,
            "currency": user.currency.symbol,
        }

    def set_safe_currency(self, user_id: str, symbol: str) -> UserRecord:
        """`POST /api/v1/safe/set-currency`, which upstream is deprecated.

        Refused once the Safe has moved: re-denominating a balance is not a
        currency change, it is a conversion, and this provider does not do one.
        """
        user = self._require_user(user_id)
        if user.spendable_units or user.held_units or self._deposits:
            raise IssuerError("the Safe currency cannot change once it holds a balance")
        user.currency = SAFE_CURRENCIES[symbol]
        return user

    def set_daily_limit(self, user_id: str, limit_minor: int | None) -> UserRecord:
        """`POST /api/v1/safe/set-daily-limit`, the Roles-module allowance.

        Per Safe, so it is shared by every card the user holds. That is the honest
        home for `CreateCardRequest.spend_limit_minor` at this provider, which has
        no per-card limit at all.
        """
        user = self._require_user(user_id)
        user.daily_limit_minor = limit_minor
        return user

    def daily_limit(self, user_id: str) -> int | None:
        return self._require_user(user_id).daily_limit_minor

    # ------------------------------------------------------------- funding ----

    def receive_onchain_deposit(
        self, safe_address: str, amount: Money, *, confirmed: bool = True
    ) -> SafeDeposit:
        """A stablecoin transfer landing in the Safe. **Not an API call.**

        This is what the bridge does in SPEC.md §5.2, and the only way money
        reaches a card at this provider. Nothing about it is ours to authorize —
        the deposit exists whether or not any funding intent asked for it.
        """
        user_id = self._safes.get(safe_address)
        if user_id is None:
            raise IssuerError(f"no Safe at this provider for address {safe_address!r}")
        user = self._users[user_id]
        if amount.currency != user.currency.code:
            raise IssuerError(
                f"the Safe holds {user.currency.symbol} ({user.currency.code}), "
                f"not {amount.currency}"
            )

        units = to_token_units(amount, user.currency)
        deposit_id = self._next("dep")
        deposit = SafeDeposit(
            deposit_id=deposit_id,
            tx_hash=self._tx_hash(deposit_id),
            safe_address=safe_address,
            amount_units=units,
            currency=user.currency,
            confirmed=confirmed,
            received_at=self._now(),
        )
        self._deposits.append(deposit)
        if confirmed:
            user.spendable_units += units
        return deposit

    def confirm_deposit(self, deposit_id: str) -> SafeDeposit:
        """Promote a deposit to `finalized`, making it spendable."""
        for deposit in self._deposits:
            if deposit.deposit_id != deposit_id:
                continue
            if not deposit.confirmed:
                deposit.confirmed = True
                self._users[
                    self._safes[deposit.safe_address]
                ].spendable_units += deposit.amount_units
            return deposit
        raise IssuerError(f"no such deposit at this provider: {deposit_id!r}")

    def deposits(self) -> tuple[SafeDeposit, ...]:
        return tuple(self._deposits)

    def attributed_deposits(self) -> tuple[SafeDeposit, ...]:
        """Deposits a funding intent has claimed."""
        return tuple(deposit for deposit in self._deposits if deposit.funding_ref is not None)

    def confirm_funding(self, card_id: str, amount: Money, funding_ref: str) -> FundingResult:
        """Verify and attribute a deposit. Moves nothing.

        The `CRYPTO_DEPOSIT` reading of `fund_card`: the bridge has (or has not)
        delivered tokens to the Safe, the provider has (or has not) seen them
        confirm, and all this call can do is say which. When no confirmed,
        unattributed deposit covers the amount the answer is `PENDING` and the
        engine waits — the money is not ours to conjure.
        """
        card = self._require_card(card_id)
        user = self._users[card.user_id]

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

        if card.card_state is CardState.CANCELED:
            raise FundingRejectedError(
                card_id, f"card is void, lost or stolen ({card.status_name})"
            )
        if amount.currency != user.currency.code:
            raise FundingRejectedError(
                card_id,
                f"the Safe is denominated in {user.currency.code}, not {amount.currency}",
            )
        if amount.amount_minor <= 0:
            raise FundingRejectedError(card_id, f"amount must be positive, got {amount}")

        units = to_token_units(amount, user.currency)
        deposit = self._claimable_deposit(user, units)
        if deposit is None:
            return FundingResult(
                provider_id=PROVIDER_ID,
                card_id=card_id,
                funding_ref=funding_ref,
                # No provider-side object exists to reference: nothing has been
                # observed yet. `None`, not `""` — see `FundingResult`.
                issuer_funding_ref=None,
                status=FundingStatus.PENDING,
                amount=amount,
                raw={
                    "safeAddress": user.safe_address,
                    "chain": CHAIN_NAME,
                    "reason": "no confirmed unattributed deposit covers this amount",
                    "fundingModel": "crypto_deposit",
                },
            )

        deposit.funding_ref = funding_ref
        result = FundingResult(
            provider_id=PROVIDER_ID,
            card_id=card_id,
            funding_ref=funding_ref,
            # The chain's reference is the provider's reference here.
            issuer_funding_ref=deposit.tx_hash,
            status=FundingStatus.SUCCEEDED,
            amount=amount,
            raw={
                "safeAddress": user.safe_address,
                "chain": CHAIN_NAME,
                "depositId": deposit.deposit_id,
                "txHash": deposit.tx_hash,
                "tokenSymbol": deposit.currency.symbol,
                "tokenUnits": str(deposit.amount_units),
                "fundingModel": "crypto_deposit",
            },
        )
        self._fundings[funding_ref] = result
        return result

    def _claimable_deposit(self, user: UserRecord, units: int) -> SafeDeposit | None:
        """Oldest confirmed, unattributed deposit that covers `units`.

        Whole-deposit attribution: a larger deposit is consumed entirely rather
        than split. Netting a bridged amount against its fees, and splitting the
        remainder, is phase 6's reconciliation problem (SPEC.md §11).
        """
        for deposit in self._deposits:
            if (
                deposit.safe_address == user.safe_address
                and deposit.confirmed
                and deposit.funding_ref is None
                and deposit.amount_units >= units
            ):
                return deposit
        return None

    def spendable(self, user_id: str) -> Money:
        user = self._require_user(user_id)
        return to_money(user.spendable_units, user.currency)

    def account_balances(self, user_id: str) -> dict[str, str]:
        """`GET /api/v1/account-balances`.

        Digit strings in token units, as their schema describes: `total` is
        `spendable` plus `pending`, and `pending` is what has arrived but is not
        yet available to spend.
        """
        user = self._require_user(user_id)
        pending = sum(
            deposit.amount_units
            for deposit in self._deposits
            if deposit.safe_address == user.safe_address and not deposit.confirmed
        )
        return {
            "total": str(user.spendable_units + pending),
            "spendable": str(user.spendable_units),
            "pending": str(pending),
        }

    # ----------------------------------------------------------- lifecycle ----

    def create_virtual_card(
        self, user_id: str, *, spend_limit_minor: int | None = None
    ) -> CardRecord:
        """`POST /api/v1/cards/virtual` -> `{cardId}`.

        422 past `MAX_ACTIVE_CARDS`, counting every card that is not void, lost or
        stolen — the only cards that free a slot.
        """
        user = self._require_user(user_id)
        if spend_limit_minor is not None:
            self.set_daily_limit(user_id, spend_limit_minor)

        active = sum(
            1
            for card in self._cards.values()
            if card.user_id == user_id and card.card_state is not CardState.CANCELED
        )
        if active >= MAX_ACTIVE_CARDS:
            raise IssuerError(
                f"user {user_id} already holds {MAX_ACTIVE_CARDS} active cards, "
                f"which is this provider's maximum"
            )

        created_at = self._now()
        card_id = self._next("crd")
        sequence = self._counters["crd"]
        card = CardRecord(
            card_id=card_id,
            card_token=self._next("ctk"),
            user_id=user.user_id,
            virtual=True,
            # Synthetic and repeating on purpose: nothing here should ever be
            # mistaken for card-number material.
            last_four=f"{(sequence * 1111) % 10_000:04d}",
            exp_month=created_at.month,
            exp_year=created_at.year + 3,
            activated_at=None,
            created_at=created_at,
        )
        self._cards[card_id] = card
        self._tokens[card.card_token] = card_id
        return card

    def get_card(self, card_id: str) -> CardRecord:
        return self._require_card(card_id)

    def card_status(self, card_id: str) -> dict[str, Any]:
        """`GET /api/v1/cards/{cardId}/status`, field for field."""
        card = self._require_card(card_id)
        return {
            "statusCode": card.status_code,
            "isFrozen": card.is_frozen,
            "isStolen": card.is_stolen,
            "isLost": card.is_lost,
            "isBlocked": card.is_blocked,
            "isVoid": card.is_void,
            "activatedAt": card.activated_at.isoformat() if card.activated_at else None,
        }

    def _refuse(self, card: CardRecord, target: CardState) -> IllegalCardTransitionError:
        return IllegalCardTransitionError(card.card_id, card.card_state, target)

    def activate_card(self, card_id: str) -> CardRecord:
        """`POST /api/v1/cards/{cardId}/activate`. 422 if already activated."""
        card = self._require_card(card_id)
        if card.card_state is CardState.CANCELED or card.activated_at is not None:
            raise self._refuse(card, CardState.ACTIVE)
        card.previous_status_name = card.status_name
        card.activated_at = self._now()
        return card

    def freeze_card(self, card_id: str) -> CardRecord:
        """`POST /api/v1/cards/{cardId}/freeze`. 400 on an illegal transition."""
        card = self._require_card(card_id)
        if card.card_state is not CardState.ACTIVE:
            raise self._refuse(card, CardState.FROZEN)
        card.previous_status_name = card.status_name
        card.is_frozen = True
        return card

    def unfreeze_card(self, card_id: str) -> CardRecord:
        """`POST /api/v1/cards/{cardId}/unfreeze`. 422 if the card is not activated."""
        card = self._require_card(card_id)
        if card.card_state is not CardState.FROZEN:
            raise self._refuse(card, CardState.ACTIVE)
        card.previous_status_name = card.status_name
        card.is_frozen = False
        return card

    def void_card(self, card_id: str) -> CardRecord:
        """`POST /api/v1/cards/{cardId}/void`. Terminal.

        Upstream this also answers 422 for "Only virtual cards can be voided".
        Unreachable here and deliberately not modelled: SPEC.md §2 puts physical
        cards out of scope, so every card this provider issues is virtual.
        """
        card = self._require_card(card_id)
        if card.card_state is CardState.CANCELED:
            raise self._refuse(card, CardState.CANCELED)
        card.previous_status_name = card.status_name
        card.is_void = True
        return card

    def report_lost(self, card_id: str) -> CardRecord:
        """`POST /api/v1/cards/{cardId}/lost`. 409 if already lost."""
        card = self._require_card(card_id)
        if card.card_state is CardState.CANCELED:
            raise self._refuse(card, CardState.CANCELED)
        card.previous_status_name = card.status_name
        card.is_lost = True
        return card

    def report_stolen(self, card_id: str) -> CardRecord:
        """`POST /api/v1/cards/{cardId}/stolen`. Terminal, like lost."""
        card = self._require_card(card_id)
        if card.card_state is CardState.CANCELED:
            raise self._refuse(card, CardState.CANCELED)
        card.previous_status_name = card.status_name
        card.is_stolen = True
        return card

    # --------------------------------------------------------- PSE reveal ----

    def mint_ephemeral_token(self, card_id: str) -> dict[str, Any]:
        """`POST https://api-pse.gnosispay.com/api/v1/ephemeral-token`.

        Upstream this call is authenticated by **mTLS**, with the partner's app id
        in the certificate CN — there is no bearer token and no shared secret. The
        token it returns lives 60 seconds and is single use; the client hands it to
        the PSE SDK, which renders card data in an iframe the client cannot read.

        It stays on the provider surface deliberately. Adding a `reveal` method to
        `CardIssuerAdapter` for one provider, with no caller until phase 8, would
        widen the interface ahead of the need (SPEC.md §9.2).
        """
        card = self._require_card(card_id)
        token = hashlib.sha256(f"{self._program}:pse:{self._next('eph')}".encode()).hexdigest()
        expires_at = self._now() + timedelta(seconds=EPHEMERAL_TOKEN_TTL_SECONDS)
        self._ephemeral[token] = (card.card_id, expires_at)
        return {"data": {"token": token, "expiresAt": expires_at.isoformat()}}

    @property
    def spent_ephemeral_tokens(self) -> frozenset[str]:
        """Tokens already redeemed. For tests and demos, never for the pipeline.

        A copy rather than the set itself: this is the provider's memory of what has
        been spent, and a caller able to `discard()` from it could replay a reveal.
        """
        return frozenset(self._spent_ephemeral)

    def redeem_ephemeral_token(self, token: str) -> dict[str, Any]:
        """What the PSE SDK gets back — minus the part it is designed to protect.

        No PAN and no CVV exist anywhere in this repo: upstream they never reach
        the partner's backend either, which is the point of the iframe.
        """
        entry = self._ephemeral.get(token)
        if entry is None:
            raise IssuerError("unknown ephemeral token")
        card_id, expires_at = entry
        # Kept rather than deleted, so a replay is reported as a replay: "already
        # used" and "never existed" are different incidents to whoever is reading.
        if token in self._spent_ephemeral:
            raise IssuerError("ephemeral token has already been redeemed; it is single use")
        if self._now() > expires_at:
            raise IssuerError("ephemeral token expired")
        self._spent_ephemeral.add(token)
        card = self._require_card(card_id)
        return {
            "cardId": card.card_id,
            "lastFourDigits": card.last_four,
            "expMonth": card.exp_month,
            "expYear": card.exp_year,
            "renderedIn": "pse-iframe",
        }

    # ------------------------------------------------------- transactions ----

    def authorize(
        self,
        card_id: str,
        amount: Money,
        *,
        merchant: str = "Test Merchant",
        city: str = "Lisbon",
        country: str = "PRT",
        mcc: str = "5814",
    ) -> TransactionRecord:
        """An authorization, and the on-chain hold it causes.

        Per their lifecycle page, on approval "money is immediately deducted from
        user account and moved to hold account on chain" — so the Safe balance
        drops at authorization time, not at clearing.
        """
        card = self._require_card(card_id)
        user = self._users[card.user_id]
        units = to_token_units(amount, user.currency)

        status = self._authorization_status(card, user, amount, units)
        approved = status == "Approved"
        if approved:
            user.spendable_units -= units
            user.held_units += units

        transaction = TransactionRecord(
            thread_id=self._next("txn"),
            card_token=card.card_token,
            user_id=user.user_id,
            kind="Payment",
            status=status,
            billing_units=units,
            currency=user.currency,
            merchant_name=merchant,
            merchant_city=city,
            merchant_country=country,
            mcc=mcc,
            is_pending=approved,
            created_at=self._now(),
        )
        self._transactions[transaction.thread_id] = transaction
        self._emit(
            PROVIDER_EVENT_TYPES["authorization"],
            self._transaction_data(transaction),
        )
        return transaction

    def _authorization_status(
        self, card: CardRecord, user: UserRecord, amount: Money, units: int
    ) -> str:
        if card.card_state is not CardState.ACTIVE:
            return "Other"
        if user.daily_limit_minor is not None and amount.amount_minor > user.daily_limit_minor:
            return "ExceedsApprovalAmountLimit"
        if user.spendable_units < units:
            return "InsufficientFunds"
        return "Approved"

    def clear(self, thread_id: str) -> Delivery:
        """The clearing record: the hold leaves for the merchant."""
        transaction = self._require_transaction(thread_id)
        if not transaction.is_pending:
            raise IssuerError(f"transaction {thread_id} has already cleared")
        user = self._users[transaction.user_id]
        user.held_units -= transaction.billing_units
        transaction.is_pending = False
        transaction.cleared_at = self._now()
        return self._emit(
            PROVIDER_EVENT_TYPES["transaction_cleared"], self._transaction_data(transaction)
        )

    def refund(self, card_id: str, amount: Money, *, merchant: str = "Test Merchant") -> Delivery:
        """A merchant credit voucher. Credits the Safe, and clears on arrival."""
        card = self._require_card(card_id)
        user = self._users[card.user_id]
        units = to_token_units(amount, user.currency)
        user.spendable_units += units

        transaction = TransactionRecord(
            thread_id=self._next("txn"),
            card_token=card.card_token,
            user_id=user.user_id,
            kind="Refund",
            # Their schema gives `status` to payments only.
            status=None,
            billing_units=units,
            currency=user.currency,
            merchant_name=merchant,
            merchant_city="Lisbon",
            merchant_country="PRT",
            mcc="5814",
            is_pending=False,
            created_at=self._now(),
            cleared_at=self._now(),
        )
        self._transactions[transaction.thread_id] = transaction
        return self._emit(
            PROVIDER_EVENT_TYPES["transaction_cleared"], self._transaction_data(transaction)
        )

    def reverse(self, thread_id: str) -> Delivery:
        """An authorization reversal: the hold goes back to the Safe."""
        original = self._require_transaction(thread_id)
        if not original.is_pending:
            raise IssuerError(f"transaction {thread_id} has already cleared")
        user = self._users[original.user_id]
        user.held_units -= original.billing_units
        user.spendable_units += original.billing_units
        original.is_pending = False

        reversal = TransactionRecord(
            thread_id=self._next("txn"),
            card_token=original.card_token,
            user_id=original.user_id,
            kind="Reversal",
            status=None,
            billing_units=original.billing_units,
            currency=original.currency,
            merchant_name=original.merchant_name,
            merchant_city=original.merchant_city,
            merchant_country=original.merchant_country,
            mcc=original.mcc,
            is_pending=False,
            created_at=self._now(),
            cleared_at=self._now(),
        )
        self._transactions[reversal.thread_id] = reversal
        return self._emit(
            PROVIDER_EVENT_TYPES["transaction_cleared"], self._transaction_data(reversal)
        )

    def dispute(self, thread_id: str, *, reason: str = "fraud") -> Delivery:
        """`POST /api/v1/transactions/{id}/dispute`, and an extension event.

        SPEC.md §3.3 requires a `chargeback` type. Gnosis Pay documents the dispute
        *endpoint* but publishes no matching webhook, so the delivery is ours — see
        `EXTENSION_EVENT_TYPES`.
        """
        transaction = self._require_transaction(thread_id)
        data = self._transaction_data(transaction)
        data["disputeReason"] = reason
        return self._emit(EXTENSION_EVENT_TYPES["chargeback"], data)

    def _transaction_data(self, transaction: TransactionRecord) -> dict[str, Any]:
        """Their `card.transaction.*` payload: the transaction under `event`."""
        currency = {
            "symbol": transaction.currency.symbol,
            "code": transaction.currency.code,
            "decimals": transaction.currency.decimals,
            "name": transaction.currency.name,
        }
        event: dict[str, Any] = {
            "threadId": transaction.thread_id,
            "cardToken": transaction.card_token,
            "kind": transaction.kind,
            "isPending": transaction.is_pending,
            "createdAt": transaction.created_at.isoformat(),
            "clearedAt": (transaction.cleared_at.isoformat() if transaction.cleared_at else None),
            "mcc": transaction.mcc,
            "transactionType": PURCHASE_TRANSACTION_TYPE,
            "merchant": {
                "name": transaction.merchant_name,
                "city": transaction.merchant_city,
                "country": transaction.merchant_country,
            },
            "billingAmount": str(transaction.billing_units),
            "billingCurrency": currency,
            "transactionAmount": str(transaction.billing_units),
            "transactionCurrency": currency,
        }
        if transaction.status is not None:
            event["status"] = transaction.status
        # Their schema names the amount after the kind, so the adapter cannot read
        # one field and be done.
        if transaction.kind == "Refund":
            event["refundAmount"] = str(transaction.billing_units)
            event["refundCurrency"] = currency
        elif transaction.kind == "Reversal":
            event["reversalAmount"] = str(transaction.billing_units)
            event["reversalCurrency"] = currency
        user = self._users[transaction.user_id]
        return {
            "userId": transaction.user_id,
            "safeWallets": [user.safe_address],
            "event": event,
        }

    # ------------------------------------------------------------ webhooks ----

    @property
    def deliveries(self) -> tuple[Delivery, ...]:
        return tuple(self._deliveries)

    def drain_deliveries(self) -> tuple[Delivery, ...]:
        """Hand over everything emitted so far and forget it."""
        drained = tuple(self._deliveries)
        self._deliveries.clear()
        return drained

    def _tx_hash(self, seed: str) -> str:
        digest = hashlib.sha256(f"{self._program}:tx:{seed}".encode()).hexdigest()
        return f"0x{digest}"

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> Delivery:
        """Sign and queue a delivery.

        The envelope is `{eventType, data}` and nothing else: no event id, and no
        timestamp — that lives only in the signed `x-webhook-timestamp` header.
        """
        payload = {"eventType": event_type, "data": dict(data)}
        # sort_keys + compact separators: the bytes are what gets signed, so they
        # must be reproducible for a given payload.
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(self._now().timestamp()))
        delivery = Delivery(
            provider_id=PROVIDER_ID,
            event_type=event_type,
            headers={
                "content-type": "application/json",
                TIMESTAMP_HEADER: timestamp,
                SIGNATURE_HEADER: sign(self._signing_key, timestamp=timestamp, body=body),
            },
            body=body,
        )
        self._deliveries.append(delivery)
        return delivery

    def emit_card_status_changed(self, card_id: str) -> Delivery:
        """`card.status.changed`: `{userId, cardToken, oldStatus, newStatus}`."""
        card = self._require_card(card_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["card_lifecycle"],
            {
                "userId": card.user_id,
                "cardToken": card.card_token,
                "oldStatus": card.previous_status_name,
                "newStatus": card.status_name,
            },
        )

    def emit_transaction_cleared(
        self, card_id: str, amount: Money, *, kind: str = "Payment"
    ) -> Delivery:
        """A cleared transaction, without an authorization to clear first.

        For tests and demos that need the delivery rather than the balance moves.
        """
        card = self._require_card(card_id)
        user = self._users[card.user_id]
        moment = self._now()
        transaction = TransactionRecord(
            thread_id=self._next("txn"),
            card_token=card.card_token,
            user_id=user.user_id,
            kind=kind,
            status="Approved" if kind == "Payment" else None,
            billing_units=to_token_units(amount, user.currency),
            currency=user.currency,
            merchant_name="Test Merchant",
            merchant_city="Lisbon",
            merchant_country="PRT",
            mcc="5814",
            is_pending=False,
            created_at=moment,
            cleared_at=moment,
        )
        self._transactions[transaction.thread_id] = transaction
        return self._emit(
            PROVIDER_EVENT_TYPES["transaction_cleared"], self._transaction_data(transaction)
        )

    def emit_three_ds_challenge(
        self,
        card_id: str,
        *,
        code: str = "123456",
        ttl_seconds: int = CHALLENGE_TTL_SECONDS,
    ) -> Delivery:
        """A 3DS challenge — an extension, not a documented Gnosis Pay event.

        SPEC.md §6 allows the mock's simulator to carry the phase-7 OTP path, and
        this provider publishes nothing that fits. See `EXTENSION_EVENT_TYPES`.

        This is the **ACS-orchestrated** shape deliberately: the provider generated
        the code and tells us what it is, which is the opposite of Lithic's
        customer-orchestrated flow, where we mint it. One of each is what makes the
        OTP service's two paths both real (docs/ARCHITECTURE.md §11.4) — and it is
        why `otpCode` is a secret arriving in a webhook body, which is the whole
        reason `CardEvent.otp_code` never serializes.

        `expiresAt` mirrors the `expiry_time` a real 3DS challenge object carries:
        the challenge has a deadline of its own and the code should not outlive it.
        """
        card = self._require_card(card_id)
        issued_at = self._now()
        challenge = ChallengeRecord(
            challenge_id=self._next("3ds"),
            card_id=card_id,
            code=code,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
        )
        self._challenges[challenge.challenge_id] = challenge
        return self._emit(
            EXTENSION_EVENT_TYPES["three_ds_challenge"],
            {
                "userId": card.user_id,
                "cardToken": card.card_token,
                "challengeId": challenge.challenge_id,
                "otpCode": challenge.code,
                "expiresAt": challenge.expires_at.isoformat(),
            },
        )

    def get_challenge(self, challenge_id: str) -> ChallengeRecord | None:
        """One issued challenge, open or answered. For tests and the demo."""
        return self._challenges.get(challenge_id)

    def answer_challenge(self, challenge_id: str, answer: str) -> ChallengeRecord:
        """Record the cardholder's decision, once.

        `POST /api/v1/cards/3ds/{id}/response` in shape — an extension, since Gnosis
        Pay publishes no 3DS surface at all. What it models is the part a real ACS
        enforces and a naive mock would not: a challenge is answerable exactly once,
        and only before it expires. Both refusals exist so the adapter above has
        something real to translate (docs/ARCHITECTURE.md §11.7).
        """
        challenge = self._challenges.get(challenge_id)
        if challenge is None:
            raise ChallengeNotFoundError(challenge_id)
        if challenge.answer is not None:
            raise ChallengeAlreadyAnsweredError(
                challenge_id, f"answered {challenge.answer} at {challenge.answered_at}"
            )
        now = self._now()
        if now >= challenge.expires_at:
            raise ChallengeAlreadyAnsweredError(
                challenge_id, f"expired at {challenge.expires_at.isoformat()}"
            )
        challenge.answer = answer
        challenge.answered_at = now
        return challenge

    def emit_user_created(self, user_id: str) -> Delivery:
        """`user.created`. An account event, not a card event: `unmapped` for us."""
        user = self._require_user(user_id)
        return self._emit(
            PROVIDER_EVENT_TYPES["user_created"],
            {
                "userId": user.user_id,
                "email": user.email,
                "safeWallets": [user.safe_address],
                "kycStatus": "approved",
            },
        )

    def emit_unknown(self, event_type: str, data: Mapping[str, Any] | None = None) -> Delivery:
        """Emit an event type we do not model, to exercise the `unmapped` path."""
        return self._emit(event_type, dict(data or {}))
