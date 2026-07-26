"""The Lithic adapter's card lifecycle (SPEC.md §3.2, §12.3).

Contract tests: every response is a fixture recorded from the real sandbox by
`scripts/record_lithic_fixtures.py`, replayed through `respx`. The suite never calls
Lithic (SPEC.md §10), and re-running the recorder is how a changed payload shows up
as a failing test rather than as a surprise in a demo.

What is worth asserting about an adapter is not that it can spell a URL. It is that
the translation is *lossless where it matters and lossy where it must be*: money
stays integer minor units, provider ids stay opaque, timestamps come back as aware
UTC, a state we do not model is refused rather than guessed at — and the PAN Lithic
hands back on card creation never reaches a DTO or the ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.money import Money
from app.issuers.base import (
    CardNotFoundError,
    CardState,
    ChallengeAlreadyAnsweredError,
    ChallengeDecision,
    ChallengeNotFoundError,
    CreateCardholderRequest,
    CreateCardRequest,
    FundingModel,
    IllegalCardTransitionError,
    IssuerError,
)
from app.issuers.lithic import LithicAdapter
from app.issuers.lithic import adapter as adapter_module
from app.issuers.lithic.client import LithicApiError, LithicClient
from app.issuers.lithic.config import LithicSettings

BASE_URL = "https://sandbox.lithic.test/v1"
API_KEY = "test-sandbox-key-not-a-real-credential"
WEBHOOK_SECRET = "whsec_cGhhc2UtMy10ZXN0LWtleS1tYXRlcmlhbC0zMmJ5dGU="

FIXTURES = Path(__file__).parent / "fixtures" / "lithic"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


CARD = fixture("card_created")
CARD_TOKEN = CARD["token"]
ACCOUNT_TOKEN = CARD["account_token"]


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def adapter() -> LithicAdapter:
    client = LithicClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0, sleep=no_sleep)
    return LithicAdapter(
        client=client,
        webhook_secret=WEBHOOK_SECRET,
        clock=lambda: datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
    )


# ------------------------------------------------------------- identity ----


def test_the_adapter_declares_itself_a_fiat_rail_issuer(adapter: LithicAdapter) -> None:
    # The other half of the taxonomy from `gnosis_pay_mock` (SPEC.md §3.2), which
    # is the reason both exist.
    assert "lithic" == adapter.provider_id
    assert FundingModel.FIAT_RAIL is adapter.funding_model


def test_a_client_builds_without_a_key_and_refuses_on_first_use() -> None:
    # The two checks below are deliberately in different places, and the pair is the
    # point (docs/ARCHITECTURE.md §8.10):
    #
    #   * an empty **API key** is refused on the request path, because it already fails
    #     loudly and correctly on its own (Lithic answers 401 "Please provide a valid API
    #     key") and because `registry.describe()` builds every adapter to answer
    #     `GET /providers` — so refusing to be *built* took that route down for the
    #     providers that were configured;
    #   * an unusable **webhook secret** is refused at construction, because it fails
    #     silently and in the wrong place: every genuine delivery comes back "invalid
    #     signature" and sends whoever debugs it to Lithic's dashboard instead of `.env`.
    #
    # Building with no key must therefore succeed. `tests/test_lithic_client.py` asserts
    # the refusal that follows on the first call.
    assert LithicClient(base_url=BASE_URL, api_key="", timeout=5.0) is not None


def test_a_webhook_secret_that_cannot_be_a_key_is_refused_at_construction() -> None:
    # Fail where the misconfiguration is, not later as "invalid signature" on every
    # genuine delivery. Contrast the API key, above.
    client = LithicClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0)
    with pytest.raises(ValueError, match="base64"):
        LithicAdapter(client=client, webhook_secret="whsec_not base64 at all!!")


def test_no_webhook_secret_is_allowed_so_the_rest_of_the_adapter_works(
    adapter: LithicAdapter,
) -> None:
    # A program with no event subscription yet still creates and funds cards; only
    # inbound deliveries are unavailable, and those fail closed.
    client = LithicClient(base_url=BASE_URL, api_key=API_KEY, timeout=5.0)
    assert LithicAdapter(client=client, webhook_secret="") is not None


# ----------------------------------------------------------- cardholders ----


@respx.mock
async def test_creating_a_cardholder_uses_the_kyc_exempt_workflow(
    adapter: LithicAdapter,
) -> None:
    recorded = fixture("account_holder_created")
    route = respx.post(f"{BASE_URL}/account_holders").mock(
        return_value=httpx.Response(200, json=recorded)
    )

    holder = await adapter.create_cardholder(
        CreateCardholderRequest(
            email="ada@example.test",
            first_name="Ada",
            last_name="Lovelace",
            external_ref="intent-42",
        )
    )

    sent = json.loads(route.calls.last.request.content)
    assert "KYC_EXEMPT" == sent["workflow"]
    assert "PREPAID_CARD_USER" == sent["kyc_exemption_type"]
    assert "intent-42" == sent["external_id"]
    # Lithic requires an address and a phone that our interface does not carry; the
    # adapter supplies sandbox-only placeholders rather than widening the DTO.
    assert {"address1", "city", "state", "postal_code", "country"} == set(sent["address"])

    # `cardholder_id` is the account token, because that is what creating a card
    # needs. The account *holder* token is kept in `raw` for support tickets.
    assert recorded["account_token"] == holder.cardholder_id
    assert recorded["token"] == holder.raw["account_holder_token"]
    assert "ada@example.test" == holder.email
    assert "ACCEPTED" == holder.state


@respx.mock
async def test_a_cardholder_timestamp_without_a_zone_is_read_as_utc(
    adapter: LithicAdapter,
) -> None:
    # Lithic returns `created` for an account holder with no offset at all
    # ("2026-07-25T06:16:47.123456"). Naive is not a legal `CardEvent` timestamp,
    # and guessing local time would move it by hours.
    respx.post(f"{BASE_URL}/account_holders").mock(
        return_value=httpx.Response(200, json=fixture("account_holder_created"))
    )

    holder = await adapter.create_cardholder(
        CreateCardholderRequest(email="a@b.test", first_name="A", last_name="B")
    )

    assert holder.created_at.tzinfo is UTC


# ----------------------------------------------------------------- cards ----


@respx.mock
async def test_creating_a_card_asks_for_a_lifetime_spend_limit(
    adapter: LithicAdapter,
) -> None:
    # `FOREVER` is what makes a spend limit behave like a balance: a MONTHLY or
    # TRANSACTION window would reset, and funding would leak away every month.
    route = respx.post(f"{BASE_URL}/cards").mock(return_value=httpx.Response(200, json=CARD))

    card = await adapter.create_card(
        ACCOUNT_TOKEN, CreateCardRequest(currency="USD", spend_limit_minor=50_000, memo="demo")
    )

    sent = json.loads(route.calls.last.request.content)
    assert "VIRTUAL" == sent["type"]
    assert ACCOUNT_TOKEN == sent["account_token"]
    assert "OPEN" == sent["state"]
    assert 50_000 == sent["spend_limit"]
    assert "FOREVER" == sent["spend_limit_duration"]
    assert "demo" == sent["memo"]

    assert CARD_TOKEN == card.card_id
    assert ACCOUNT_TOKEN == card.cardholder_id
    assert CardState.ACTIVE is card.state
    assert CARD["last_four"] == card.last_four
    assert 7 == card.exp_month
    assert 2031 == card.exp_year
    assert "USD" == card.currency
    assert 50_000 == card.spend_limit_minor
    # A fiat rail has nowhere to send a deposit (SPEC.md §3.2).
    assert card.deposit_address is None
    assert card.created_at.tzinfo is UTC


@respx.mock
async def test_card_creation_is_idempotent_on_a_key_derived_from_the_request(
    adapter: LithicAdapter,
) -> None:
    # Lithic supports `Idempotency-Key` on exactly two endpoints and this is one of
    # them: a retried create must not leave two cards on a cardholder's account.
    route = respx.post(f"{BASE_URL}/cards").mock(return_value=httpx.Response(200, json=CARD))
    request = CreateCardRequest(currency="USD", spend_limit_minor=50_000, memo="demo")

    await adapter.create_card(ACCOUNT_TOKEN, request)
    await adapter.create_card(ACCOUNT_TOKEN, request)
    await adapter.create_card(ACCOUNT_TOKEN, CreateCardRequest(spend_limit_minor=1_000))

    keys = [call.request.headers["idempotency-key"] for call in route.calls]
    assert keys[0] == keys[1], "the same request must reuse the key"
    assert keys[0] != keys[2], "a different request must not"
    assert all(len(key) == 36 for key in keys), "Lithic requires a UUID"


@respx.mock
async def test_a_card_with_no_spend_limit_is_created_without_one(
    adapter: LithicAdapter,
) -> None:
    route = respx.post(f"{BASE_URL}/cards").mock(return_value=httpx.Response(200, json=CARD))

    await adapter.create_card(ACCOUNT_TOKEN, CreateCardRequest())

    sent = json.loads(route.calls.last.request.content)
    assert "spend_limit" not in sent
    assert "spend_limit_duration" not in sent


async def test_a_zero_spend_limit_is_refused_because_lithic_reads_it_as_unlimited(
    adapter: LithicAdapter,
) -> None:
    # The trap: `spend_limit: 0` means *no limit* at Lithic, the exact opposite of
    # "this card may spend nothing". Refuse rather than create a card that can
    # spend without bound.
    with pytest.raises(IssuerError, match="unlimited"):
        await adapter.create_card(ACCOUNT_TOKEN, CreateCardRequest(spend_limit_minor=0))


async def test_a_negative_spend_limit_is_refused(adapter: LithicAdapter) -> None:
    with pytest.raises(IssuerError):
        await adapter.create_card(ACCOUNT_TOKEN, CreateCardRequest(spend_limit_minor=-1))


@respx.mock
async def test_the_pan_lithic_returns_never_reaches_the_card_dto(
    adapter: LithicAdapter,
) -> None:
    # Sandbox `POST /cards` answers with `pan` and `cvv`. `Card` has no field for
    # them — but `raw` is a free-form dict, so copying the response wholesale would
    # carry a card number into the ledger's payload column. `raw` is an allowlist.
    respx.post(f"{BASE_URL}/cards").mock(return_value=httpx.Response(200, json=CARD))
    assert CARD["pan"], "the fixture must actually contain one for this to prove anything"

    card = await adapter.create_card(ACCOUNT_TOKEN, CreateCardRequest(spend_limit_minor=50_000))

    serialized = card.model_dump_json()
    assert CARD["pan"] not in serialized
    assert CARD["cvv"] not in serialized
    assert not {"pan", "cvv"} & set(card.raw)


@respx.mock
async def test_reading_a_card_back(adapter: LithicAdapter) -> None:
    recorded = fixture("card_read_back")
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json=recorded)
    )

    card = await adapter.get_card(CARD_TOKEN)

    assert CARD_TOKEN == card.card_id
    assert recorded["memo"] == card.raw["memo"]
    assert "FOREVER" == card.raw["spend_limit_duration"]


@respx.mock
async def test_an_unknown_card_is_a_card_not_found_error(adapter: LithicAdapter) -> None:
    respx.get(f"{BASE_URL}/cards/nope").mock(
        return_value=httpx.Response(404, json=fixture("error_card_not_found"))
    )

    with pytest.raises(CardNotFoundError) as caught:
        await adapter.get_card("nope")

    assert "nope" == caught.value.card_id


@respx.mock
async def test_a_token_lithic_will_not_even_parse_is_also_not_found(
    adapter: LithicAdapter,
) -> None:
    # Lithic answers 400 "card_token must be a valid UUID" for a malformed id. We
    # treat provider ids as opaque, so we cannot pre-validate the shape — and from
    # the caller's side "not a token of ours" and "no such card" are the same thing.
    respx.get(f"{BASE_URL}/cards/not-a-uuid").mock(
        return_value=httpx.Response(400, json=fixture("error_invalid_uuid"))
    )

    with pytest.raises(CardNotFoundError):
        await adapter.get_card("not-a-uuid")


@respx.mock
async def test_a_card_state_we_do_not_model_is_refused_rather_than_guessed(
    adapter: LithicAdapter,
) -> None:
    # Calling an unrecognized state "unactivated" would be a lie about whether a
    # card can spend. Providers do add states; this must be a decision, not a default.
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json={**CARD, "state": "PENDING_REVIEW"})
    )

    with pytest.raises(IssuerError, match="PENDING_REVIEW"):
        await adapter.get_card(CARD_TOKEN)


# ------------------------------------------------------------- lifecycle ----


@pytest.mark.parametrize(
    ("action", "wanted_state", "fixture_name", "expected"),
    [
        ("freeze_card", "PAUSED", "card_paused", CardState.FROZEN),
        ("activate_card", "OPEN", "card_reopened", CardState.ACTIVE),
        ("cancel_card", "CLOSED", "card_closed", CardState.CANCELED),
    ],
)
@respx.mock
async def test_each_lifecycle_call_patches_the_state_lithic_understands(
    adapter: LithicAdapter,
    action: str,
    wanted_state: str,
    fixture_name: str,
    expected: CardState,
) -> None:
    route = respx.patch(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json=fixture(fixture_name))
    )

    card = await getattr(adapter, action)(CARD_TOKEN)

    assert {"state": wanted_state} == json.loads(route.calls.last.request.content)
    assert expected is card.state


@respx.mock
async def test_touching_a_closed_card_is_an_illegal_transition_not_a_bad_gateway(
    adapter: LithicAdapter,
) -> None:
    # Lithic answers 405 "Card not active / cannot modify a closed card". That is a
    # caller mistake (409), not a provider failure (502) — and the error has to name
    # the state the card is actually in, which costs one extra read.
    respx.patch(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(405, json=fixture("error_card_closed"))
    )
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json=fixture("card_closed"))
    )

    with pytest.raises(IllegalCardTransitionError) as caught:
        await adapter.activate_card(CARD_TOKEN)

    error = caught.value
    assert CARD_TOKEN == error.card_id
    assert CardState.CANCELED is error.from_state
    assert CardState.ACTIVE is error.to_state


@respx.mock
async def test_a_lifecycle_call_on_a_card_that_is_gone_is_not_found(
    adapter: LithicAdapter,
) -> None:
    respx.patch(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(404, json=fixture("error_card_not_found"))
    )

    with pytest.raises(CardNotFoundError):
        await adapter.freeze_card(CARD_TOKEN)


@respx.mock
async def test_a_refusal_we_cannot_explain_stays_a_provider_error(
    adapter: LithicAdapter,
) -> None:
    # If the follow-up read fails too, do not invent a `from_state`.
    respx.patch(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(405, json=fixture("error_card_closed"))
    )
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(404, json=fixture("error_card_not_found"))
    )

    with pytest.raises(IssuerError) as caught:
        await adapter.freeze_card(CARD_TOKEN)

    assert not isinstance(caught.value, IllegalCardTransitionError)


# ---------------------------------------------- responses we cannot trust ----


@respx.mock
@pytest.mark.parametrize("missing", ("token", "account_token", "last_four", "state"))
async def test_a_card_response_missing_a_field_we_depend_on_is_refused(
    adapter: LithicAdapter, missing: str
) -> None:
    # Half a card is worse than none: it would be stored, ledgered and shown.
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json={k: v for k, v in CARD.items() if k != missing})
    )

    with pytest.raises(IssuerError, match=missing):
        await adapter.get_card(CARD_TOKEN)


@respx.mock
async def test_an_expiry_that_is_not_a_number_is_refused(adapter: LithicAdapter) -> None:
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json={**CARD, "exp_month": "Jul"})
    )

    with pytest.raises(IssuerError, match="exp_month"):
        await adapter.get_card(CARD_TOKEN)


@respx.mock
async def test_a_missing_spend_limit_reads_as_no_limit_not_as_zero(
    adapter: LithicAdapter,
) -> None:
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json={**CARD, "spend_limit": "50000"})
    )

    card = await adapter.get_card(CARD_TOKEN)

    # A string where an integer belongs is not silently coerced: money is int-only.
    assert card.spend_limit_minor is None


@respx.mock
@pytest.mark.parametrize("created", [None, "", "the fifth of July", 1_700_000_000])
async def test_an_unreadable_timestamp_falls_back_to_our_clock(
    adapter: LithicAdapter, created: object
) -> None:
    # Refusing the whole card over a malformed timestamp would be worse than
    # recording when we saw it; either way the value stays aware UTC.
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json={**CARD, "created": created})
    )

    card = await adapter.get_card(CARD_TOKEN)

    assert datetime(2026, 7, 25, 12, 0, tzinfo=UTC) == card.created_at


@respx.mock
async def test_an_offset_timestamp_is_converted_rather_than_relabelled(
    adapter: LithicAdapter,
) -> None:
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(200, json={**CARD, "created": "2026-07-25T14:00:00+02:00"})
    )

    card = await adapter.get_card(CARD_TOKEN)

    assert datetime(2026, 7, 25, 12, 0, tzinfo=UTC) == card.created_at


@respx.mock
async def test_a_provider_failure_we_cannot_classify_stays_a_provider_failure(
    adapter: LithicAdapter,
) -> None:
    # 422 is a bad request of ours, not a missing card. Do not translate it into one.
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(
        return_value=httpx.Response(422, json=fixture("error_idempotency_key_reused"))
    )

    with pytest.raises(IssuerError) as caught:
        await adapter.get_card(CARD_TOKEN)

    assert not isinstance(caught.value, CardNotFoundError)


# ------------------------------------------------------- built from settings ----


def configure(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Point `from_settings` at explicit settings, whatever `.env` holds.

    Patches the *importing* module's attribute: `adapter.py` did
    `from ... import get_lithic_settings`, so patching the config module would not
    be seen.
    """
    monkeypatch.setattr(
        adapter_module,
        "get_lithic_settings",
        lambda: LithicSettings(**overrides),  # type: ignore[arg-type]
    )


def test_from_settings_builds_against_the_configured_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch, api_key=API_KEY, api_base_url=BASE_URL, webhook_secret=WEBHOOK_SECRET)

    built = LithicAdapter.from_settings()

    assert "lithic" == built.provider_id


async def test_from_settings_builds_without_a_key_and_fails_at_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What this test always said it was checking: the other providers must keep working
    # on a machine with no Lithic credentials at all, so the failure lands "at the one
    # call that needs them and nowhere earlier". It used to assert the opposite of its
    # own comment — that `from_settings()` raises — which is construction, not a call,
    # and is what `registry.describe()` invokes (§8.6, §8.10).
    configure(monkeypatch, api_key="")

    adapter = LithicAdapter.from_settings()
    assert adapter.provider_id == "lithic"

    with pytest.raises(ValueError, match="LITHIC_API_KEY"):
        # Refused before any request is built, so this touches no network.
        await adapter.get_card("card_whatever")


def test_the_base_url_defaults_to_the_sandbox() -> None:
    # A misconfigured environment must not be able to reach production by omission.
    assert "sandbox.lithic.com" in LithicSettings(api_key=API_KEY).api_base_url


def test_the_env_prefix_keeps_the_variable_names_the_operator_already_knows() -> None:
    # The field is `api_key`, but the variable stays `LITHIC_API_KEY`: moving this
    # config out of core/ is an internal ownership change, not a migration anyone
    # deploying has to perform.
    assert "LITHIC_" == LithicSettings.model_config["env_prefix"]


@respx.mock
async def test_money_from_lithic_is_integer_minor_units(adapter: LithicAdapter) -> None:
    respx.get(f"{BASE_URL}/cards/{CARD_TOKEN}").mock(return_value=httpx.Response(200, json=CARD))

    card = await adapter.get_card(CARD_TOKEN)

    assert card.spend_limit_minor is not None
    assert isinstance(card.spend_limit_minor, int)
    assert Money(card.spend_limit_minor, card.currency) == Money(50_000, "USD")


# ----------------------------------------------------------- 3DS / OTP ----
# SPEC.md §6.5's "where the sandbox supports it" case. Lithic really has this
# endpoint — it is the other end of the three_ds_authentication.challenge webhook,
# and their customer-orchestrated flow means the card program is the party that
# answers. The request shape below comes from the OpenAPI document their reference
# page embeds, not from its prose; see §11.7 for where the two disagree.

CHALLENGE_RESPONSE_PATH = f"{BASE_URL}/three_ds_decisioning/challenge_response"
CHALLENGE_TOKEN = "9f7b1d2e-4c3a-4f58-9c11-2a6d5e8b7c04"


@respx.mock
async def test_approving_a_challenge_sends_their_approve_value(adapter: LithicAdapter) -> None:
    route = respx.post(CHALLENGE_RESPONSE_PATH).mock(return_value=httpx.Response(200))

    response = await adapter.respond_to_challenge(CHALLENGE_TOKEN, ChallengeDecision.APPROVE)

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert {"token": CHALLENGE_TOKEN, "challenge_response": "APPROVE"} == sent
    assert ChallengeDecision.APPROVE is response.decision
    assert "lithic" == response.provider_id


@respx.mock
async def test_declining_sends_decline_by_customer_not_decline(adapter: LithicAdapter) -> None:
    """The value is `DECLINE_BY_CUSTOMER`, and this is the test that pins it.

    Their reference page's prose says `DECLINE`; the `challenge-response` schema in
    the OpenAPI document that same page embeds says `DECLINE_BY_CUSTOMER`, and the
    schema is the wire format. Sending the wrong one is a 400 on a challenge with
    minutes to live — so the enum is worth an assertion of its own (§11.7).
    """
    route = respx.post(CHALLENGE_RESPONSE_PATH).mock(return_value=httpx.Response(200))

    await adapter.respond_to_challenge(CHALLENGE_TOKEN, ChallengeDecision.DECLINE)

    sent = json.loads(route.calls.last.request.content)
    assert "DECLINE_BY_CUSTOMER" == sent["challenge_response"]


@respx.mock
async def test_a_bodyless_acknowledgement_is_a_success(adapter: LithicAdapter) -> None:
    # "Challenge Response was received and forwarded to the ACS", with no response
    # content. The first endpoint here that answers 200 with nothing at all, and
    # until phase 7 the client read that as a malformed success.
    respx.post(CHALLENGE_RESPONSE_PATH).mock(return_value=httpx.Response(200, content=b""))

    response = await adapter.respond_to_challenge(CHALLENGE_TOKEN, ChallengeDecision.APPROVE)

    # Nothing to point at, and saying so rather than inventing a reference.
    assert response.provider_ref is None
    assert {} == response.raw


@respx.mock
async def test_an_unknown_challenge_token_is_not_found(adapter: LithicAdapter) -> None:
    """Their 404 has **no body at all**, which is not what the docs imply.

    `error_challenge_not_found.json` is what the recorder captured from the real
    sandbox: `_non_json_body: ""`, its marker for a response that is not JSON. Every
    other Lithic error here answers with a `message` and a `debugging_request_id`,
    and the reference page describes this one as "The provided token was not found"
    — which reads like an error object. It is an empty body (§11.7).

    So the failure path has to work without one, and this is the test that says so.
    """
    recorded = fixture("error_challenge_not_found")
    assert "" == recorded["_non_json_body"], "the recorded 404 carried no body"
    respx.post(CHALLENGE_RESPONSE_PATH).mock(return_value=httpx.Response(404, content=b""))

    with pytest.raises(ChallengeNotFoundError) as raised:
        await adapter.respond_to_challenge(CHALLENGE_TOKEN, ChallengeDecision.APPROVE)

    assert CHALLENGE_TOKEN == raised.value.challenge_id
    assert raised.value.retryable is False


@respx.mock
async def test_a_challenge_the_acs_has_closed_is_already_answered(adapter: LithicAdapter) -> None:
    # Their 422: "response already set or status updated by upstream 3DS Service" —
    # a timeout at the ACS, too many attempts, or a second answer. A fact about the
    # challenge, not a provider failure, so it must not surface as a 502.
    #
    # This body is built from their `challenge-response-unprocessable` schema, which
    # requires a `message`, rather than recorded: a real 422 needs a real challenge,
    # and raising one needs the program configured for Out of Band challenges — a
    # dashboard setting, not an API call. Said plainly because a documented shape is
    # weaker evidence than a recorded one (§11.7).
    respx.post(CHALLENGE_RESPONSE_PATH).mock(
        return_value=httpx.Response(422, json={"message": "challenge already completed"})
    )

    with pytest.raises(ChallengeAlreadyAnsweredError, match="already completed"):
        await adapter.respond_to_challenge(CHALLENGE_TOKEN, ChallengeDecision.APPROVE)


@respx.mock
async def test_a_provider_failure_stays_a_provider_failure(adapter: LithicAdapter) -> None:
    # Everything that is not 404 or 422 is theirs and travels unchanged, so the API
    # answers 502 rather than pretending the challenge is gone.
    respx.post(CHALLENGE_RESPONSE_PATH).mock(
        return_value=httpx.Response(400, json={"message": "malformed token"})
    )

    with pytest.raises(LithicApiError):
        await adapter.respond_to_challenge(CHALLENGE_TOKEN, ChallengeDecision.APPROVE)
