"""Record the Stripe Issuing contract-test fixtures from a live test-mode account.

    python scripts/record_stripe_fixtures.py            # write tests/fixtures/stripe_issuing/
    python scripts/record_stripe_fixtures.py --dry-run  # walk, print, write nothing
    python scripts/record_stripe_fixtures.py --preflight # check the account, walk nothing

Needs `STRIPE_ISSUING_API_KEY` (a test key — this refuses a live one). **The test
suite never runs this** — it replays what this wrote (SPEC.md §10: tests never call a
live sandbox API). Re-run it when Stripe changes a payload, and the diff is the
change.

**Why this script exists at all.** Phase 4 was built with no Stripe credentials, so
the fixtures it shipped were hand-authored from Stripe's published example objects.
That is weaker evidence than phase 3 had, and `docs/ARCHITECTURE.md` §8.2 and §8.9 say
so. What this replaces them with is evidence about the provider rather than about our
reading of their documentation. Where a recorded shape differs from the authored one,
**the recorded one is right and the adapter is what changes.**

It deliberately talks to Stripe with plain `httpx` and literal paths and literal
bracketed form keys, instead of going through `app.issuers.stripe_issuing`. Fixtures
recorded *by* the adapter could only ever confirm the adapter agrees with itself;
recorded independently, they are evidence, and the contract tests compare the two.
That is also why the bracket syntax is spelled out here rather than imported from
`client.form_encode` — what you see is what goes on the wire.

`GET /v1/events` at the end is the valuable part: those are real Event envelopes, and
an Event envelope is exactly what a webhook delivers. So the webhook fixtures get
verified without needing a public URL or an endpoint secret. (The one thing this
script still cannot settle is the *signature* scheme in §8.2, which needs an actual
inbound delivery and `STRIPE_ISSUING_WEBHOOK_SECRET`.)

Card numbers and CVCs are replaced with obvious synthetic values before anything is
written, and the account id is replaced with a placeholder: test card numbers are not
credentials, but card-number-shaped material and an account identifier have no
business in a tracked portfolio repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.issuers.stripe_issuing.config import get_stripe_issuing_settings

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "stripe_issuing"

REDACTED_NUMBER = "4111111111111111"
REDACTED_CVC = "123"
REDACTED_ACCOUNT = "acct_stablecard000001"

#: Stripe's Issuing test helpers settle synchronously, unlike Lithic's simulations —
#: but an authorization has to exist before it can be captured, so the walk is
#: strictly ordered rather than polled.
CURRENCY = "usd"

#: The only spending-limit interval that behaves like a balance (see the adapter).
ALL_TIME = "all_time"

#: The card is created spend-limited to this, then funded, so the fixtures show a
#: limit being raised rather than set.
INITIAL_LIMIT_MINOR = 100_000
FUNDING_MINOR = 2_500

#: Amounts for the four simulated purchases: two captured (so the transaction list
#: has two pages and the cursor gets exercised), one reversed, one left pending so
#: `get_balance` has a hold to subtract.
CAPTURE_MINOR = 1_234
SECOND_CAPTURE_MINOR = 250
REVERSE_MINOR = 500
PENDING_MINOR = 3_000

#: Merchant categories confirmed present in Stripe's enum by a successful call.
#: Their published example objects use names that are not all real —
#: `hotels_motels_resorts` is rejected — so these are the ones actually verified.
CATEGORY_SOFTWARE = "computer_software_stores"
CATEGORY_TAXI = "taxicabs_limousines"
CATEGORY_RESTAURANT = "eating_places_restaurants"

#: How many times to re-attempt a declined test authorization.
#:
#: Stripe declines some attempts with `cardholder_verification_required` on a program
#: that has no real-time authorization endpoint: it wants the issuer to verify the
#: cardholder inside a two-second window, and there is nobody listening. Which attempt
#: gets flagged is not deterministic, so recording is a retry rather than a guarantee
#: (docs/ARCHITECTURE.md §8.10).
AUTHORIZATION_ATTEMPTS = 4

#: Waits before re-sending a request Stripe rate-limited.
RATE_LIMIT_BACKOFF_SECONDS = (2.0, 5.0, 15.0)

#: Every ledger-visible thing this script creates is tagged, so a later reader of the
#: account can tell fixture recording from a demo run.
RUN_TAG = "record_stripe_fixtures"

#: Fixed so a re-run produces a byte-identical fixture rather than a spurious diff.
#: Stripe wants Unix seconds; a sandbox has no real moment of acceptance.
TERMS_ACCEPTED_AT = 1785000000

#: What cannot be recorded without a funded Issuing balance, and therefore stays
#: hand-authored from Stripe's published examples until one exists.
SKIPPED_WITHOUT_FUNDS = (
    "authorization_created",
    "authorization_captured",
    "authorization_reversed",
    "authorization_pending",
    "transactions_all",
    "transactions_page_1",
    "transactions_page_2",
    "authorizations_pending",
)


class Recorder:
    """Calls Stripe and writes each response verbatim, under a stable name."""

    def __init__(self, client: httpx.AsyncClient, *, dry_run: bool) -> None:
        self.client = client
        self.dry_run = dry_run
        self.written: list[str] = []

    async def call(
        self,
        method: str,
        path: str,
        *,
        name: str | None = None,
        form: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expect: int = 200,
        quiet: bool = False,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = await self._send(method, path, form, params, headers)
        for pause in RATE_LIMIT_BACKOFF_SECONDS:
            if response.status_code != 429:
                break
            print(f"  [429] {method} {path} — rate limited, waiting {pause}s")
            await asyncio.sleep(pause)
            response = await self._send(method, path, form, params, headers)

        try:
            payload = response.json()
        except ValueError:
            payload = {"_non_json_body": response.text}
        matched = response.status_code == expect
        if not quiet:
            flag = "" if matched else f"  (expected {expect})"
            print(f"  [{response.status_code}] {method} {path}{flag}")
        if name and matched:
            self.write(name, payload)
        elif name:
            # Load-bearing: without this an unexpected 400 replaces a good fixture
            # with an error body, and the suite then "passes" against a recording of
            # our own bug. Learned the hard way on this script's first real run.
            print(f"       NOT written: {name}.json keeps its previous contents")
        if not matched and not quiet:
            print(f"       stripe said: {_message(payload)}")
        return dict(payload) if isinstance(payload, dict) else {"_root": payload}

    async def _send(
        self,
        method: str,
        path: str,
        form: dict[str, Any] | None,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        return await self.client.request(
            method,
            path,
            data={key: _form_value(value) for key, value in (form or {}).items()} or None,
            params=params,
            headers=headers,
        )

    def write(self, name: str, payload: Any) -> None:
        self.written.append(name)
        if self.dry_run:
            return
        FIXTURES.mkdir(parents=True, exist_ok=True)
        text = json.dumps(redact(payload), indent=2, sort_keys=True) + "\n"
        (FIXTURES / f"{name}.json").write_text(text)


def _form_value(value: Any) -> str:
    """One form value. Booleans are Stripe's spelling; everything else is `str`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def redact(payload: Any) -> Any:
    """Strip card-number material and the account id, leaving everything else verbatim."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "number" and isinstance(value, str):
                out[key] = REDACTED_NUMBER
            elif key == "cvc" and isinstance(value, str):
                out[key] = REDACTED_CVC
            else:
                out[key] = redact(value)
        return out
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    if isinstance(payload, str):
        # Appears inside `request_log_url` on every error body.
        return re.sub(r"acct_[A-Za-z0-9]+", REDACTED_ACCOUNT, payload)
    return payload


# --------------------------------------------------------------- preflight ----


async def preflight(rec: Recorder) -> bool:
    """Check the account, and report whether purchases can be simulated.

    Three separate prerequisites, each with its own fix, because "400 invalid
    request" is a useless thing to be told by a recording script. Two of them are
    fatal; the Issuing balance is not, because everything except the purchase walk
    works without it and a partial recording beats none.
    """
    print("preflight")

    balance = await rec.call("GET", "/balance", quiet=True)
    if balance.get("object") != "balance":
        raise SystemExit(
            "  FAIL: the API key does not work. Check STRIPE_ISSUING_API_KEY in .env "
            f"(Stripe said: {_message(balance)!r})"
        )
    if balance.get("livemode") is not False:
        raise SystemExit("  FAIL: that is a live-mode key. This project is sandbox-only.")
    print("  ok: key works, test mode")

    cards = await rec.call("GET", "/issuing/cards", params={"limit": 1}, quiet=True)
    if "error" in cards:
        raise SystemExit(
            "  FAIL: Issuing is not activated on this account, so every /v1/issuing/* "
            "call is a 400.\n"
            "        Activate it at https://dashboard.stripe.com/issuing/overview "
            "(test mode), then re-run.\n"
            f"        Stripe said: {_message(cards)!r}"
        )
    print("  ok: Issuing is activated")

    issuing = balance.get("issuing")
    available = 0
    if isinstance(issuing, dict):
        entries = issuing.get("available")
        if isinstance(entries, list):
            available = sum(
                entry.get("amount", 0)
                for entry in entries
                if isinstance(entry, dict) and entry.get("currency") == CURRENCY
            )
    if available <= 0:
        # Verified rather than assumed: with a zero balance a test-helper
        # authorization comes back `approved: false`, `status: closed`, with
        # `request_history[0].reason == "insufficient_funds"`, and the capture then
        # fails because capture needs an approved authorization in `pending`. So the
        # purchase fixtures would record declines while looking like they recorded
        # purchases — worse than not recording them.
        print("  WARNING: the Issuing balance is empty.")
        print("    Simulated authorizations will decline with `insufficient_funds`, so")
        print("    the purchase walk is SKIPPED and these stay hand-authored:")
        for name in SKIPPED_WITHOUT_FUNDS:
            print(f"      - {name}.json")
        print("    Add test funds in the Dashboard (Issuing -> Balance -> Add funds;")
        print("    `POST /v1/topups` with `destination_balance=issuing` is accepted but")
        print("    stays `pending` in test mode), then re-run to replace them.")
        return False
    print(f"  ok: Issuing balance is {available} minor units")
    return True


def _message(payload: Any) -> str:
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])
    return "no error message"


# -------------------------------------------------------------------- walk ----


async def walk(rec: Recorder, *, funded: bool) -> None:
    print("\ncardholder")
    holder = await rec.call(
        "POST",
        "/issuing/cardholders",
        name="cardholder_created",
        form={
            "type": "individual",
            "name": "Ada Lovelace",
            "email": "ada@example.test",
            "phone_number": "+15555550123",
            "billing[address][line1]": "1 Analytical Engine Way",
            "billing[address][city]": "New York",
            "billing[address][state]": "NY",
            "billing[address][postal_code]": "10128",
            "billing[address][country]": "US",
            "individual[first_name]": "Ada",
            "individual[last_name]": "Lovelace",
            # Without these the cardholder sits at `requirements.past_due` and every
            # activation fails. Mirrors what the adapter sends.
            "individual[card_issuing][user_terms_acceptance][ip]": "127.0.0.1",
            "individual[card_issuing][user_terms_acceptance][date]": TERMS_ACCEPTED_AT,
            "metadata[stablecard_external_ref]": "user-42",
            "metadata[stablecard_run]": RUN_TAG,
        },
    )
    holder_id = _require(holder, "id", "cardholder")

    print("\ncard, created inactive with an all_time limit")
    create_key = str(uuid.uuid4())
    create_form = {
        "cardholder": holder_id,
        "currency": CURRENCY,
        "type": "virtual",
        "status": "inactive",
        "spending_controls[spending_limits][0][amount]": INITIAL_LIMIT_MINOR,
        "spending_controls[spending_limits][0][interval]": "all_time",
        "metadata[stablecard_memo]": "StableCard Rail fixture",
        "metadata[stablecard_run]": RUN_TAG,
    }
    card = await rec.call(
        "POST",
        "/issuing/cards",
        name="card_created",
        form=create_form,
        idempotency_key=create_key,
    )
    card_id = _require(card, "id", "card")

    print("\nthe same create, replayed under the same idempotency key")
    replayed = await rec.call(
        "POST",
        "/issuing/cards",
        name="card_created_replayed",
        form=create_form,
        idempotency_key=create_key,
    )
    if replayed.get("id") != card_id:
        print(f"  !! replay produced a different card: {replayed.get('id')} != {card_id}")

    print("\nthe same key with a different body")
    # Documented as an error; which status and code is exactly the sort of thing worth
    # recording rather than assuming.
    await rec.call(
        "POST",
        "/issuing/cards",
        name="error_idempotency_key_reused",
        form={**create_form, "metadata[stablecard_memo]": "a different memo"},
        idempotency_key=create_key,
        expect=400,
    )

    print("\ncard read back")
    await rec.call("GET", f"/issuing/cards/{card_id}", name="card_read_back")

    print("\nactivated, with the marker that says it has ever been activated")
    await rec.call(
        "POST",
        f"/issuing/cards/{card_id}",
        name="card_activated",
        form={
            "status": "active",
            "metadata[stablecard_activated_at]": "2026-07-25T18:00:00+00:00",
            "metadata[stablecard_memo]": "StableCard Rail fixture",
            "metadata[stablecard_run]": RUN_TAG,
        },
    )

    print("\nfrozen — no metadata in the body, so the marker must survive")
    frozen = await rec.call(
        "POST", f"/issuing/cards/{card_id}", name="card_frozen", form={"status": "inactive"}
    )
    _check_marker_survived(frozen)

    print("\nactive again (the unfreeze path)")
    await rec.call(
        "POST",
        f"/issuing/cards/{card_id}",
        name="card_unfrozen",
        form={
            "status": "active",
            "metadata[stablecard_activated_at]": "2026-07-25T18:05:00+00:00",
            "metadata[stablecard_memo]": "StableCard Rail fixture",
            "metadata[stablecard_run]": RUN_TAG,
        },
    )

    print("\nfunded: the all_time limit raised, with the funding ref recorded on the card")
    await rec.call(
        "POST",
        f"/issuing/cards/{card_id}",
        name="card_funded",
        form={
            "spending_controls[spending_limits][0][amount]": INITIAL_LIMIT_MINOR + FUNDING_MINOR,
            "spending_controls[spending_limits][0][interval]": "all_time",
            "metadata[stablecard_funding_ref]": "intent-1",
            "metadata[stablecard_funding_amount]": str(FUNDING_MINOR),
            "metadata[stablecard_activated_at]": "2026-07-25T18:05:00+00:00",
            "metadata[stablecard_memo]": "StableCard Rail fixture",
            "metadata[stablecard_run]": RUN_TAG,
        },
        idempotency_key=str(uuid.uuid4()),
    )

    if funded:
        await walk_purchases(rec, card_id)
        await walk_balance_lists(rec, card_id)
    else:
        print("\npurchases and the balance lists: SKIPPED, empty Issuing balance")

    print("\ncanceled, and a change refused on it")
    await rec.call(
        "POST", f"/issuing/cards/{card_id}", name="card_canceled", form={"status": "canceled"}
    )
    await rec.call(
        "POST",
        f"/issuing/cards/{card_id}",
        name="error_card_canceled",
        form={"status": "active"},
        expect=400,
    )

    await walk_errors(rec)
    derive_variants(rec)
    await walk_events(rec)


async def authorize(
    rec: Recorder, card_id: str, amount: int, merchant: str, category: str, *, name: str | None
) -> dict[str, Any] | None:
    """One approved test authorization, retried past a verification decline.

    Returns `None` if every attempt was declined, so the caller can carry on and
    report which fixtures went unrecorded rather than dying half way through a walk.
    The first decline is written out under its own name: an authorization that was
    *refused* is a shape the adapter has to read too, and it is the only place
    `request_history` is populated.
    """
    for attempt in range(AUTHORIZATION_ATTEMPTS):
        authorization = await rec.call(
            "POST",
            "/test_helpers/issuing/authorizations",
            name=name,
            form={
                "card": card_id,
                "amount": amount,
                "currency": CURRENCY,
                "merchant_data[name]": merchant,
                "merchant_data[category]": category,
            },
        )
        if authorization.get("approved") is True:
            return authorization
        if "error" in authorization:
            # A malformed request, not a decline. Retrying an invalid parameter four
            # times only makes the same mistake more slowly — and writing the error
            # body as `authorization_declined` would be recording our own bug as
            # evidence about Stripe.
            print("       request rejected, not declined: giving up on this one")
            return None
        reasons = [
            entry.get("reason")
            for entry in authorization.get("request_history", [])
            if isinstance(entry, dict)
        ]
        print(f"       declined ({reasons}); attempt {attempt + 1}/{AUTHORIZATION_ATTEMPTS}")
        if attempt == 0:
            # A refused attempt is a shape the adapter has to read too, and the only
            # place `request_history` is populated.
            rec.write("authorization_declined", authorization)
            print("       wrote authorization_declined.json")
    return None


async def walk_purchases(rec: Recorder, card_id: str) -> None:
    """Four simulated purchases: two captured, one reversed, one left pending.

    Between them they produce every number `get_balance` has to add up, and the
    authorization lifecycle every webhook mapping in §8.7 depends on. Two captures
    rather than one so the transaction list really has two pages.
    """
    print("\nauthorization, then its capture")
    captured = await authorize(
        rec,
        card_id,
        CAPTURE_MINOR,
        "WWWW.BROWSEBUG.BIZ",
        CATEGORY_SOFTWARE,
        name="authorization_created",
    )
    if captured is not None:
        await rec.call(
            "POST",
            f"/test_helpers/issuing/authorizations/{_require(captured, 'id', 'authorization')}"
            f"/capture",
            name="authorization_captured",
        )

    print("\na second purchase, so the transaction list has two pages")
    second = await authorize(
        rec,
        card_id,
        SECOND_CAPTURE_MINOR,
        "COFFEE BY THE PARK",
        "eating_places_restaurants",
        name=None,
    )
    if second is not None:
        await rec.call(
            "POST",
            f"/test_helpers/issuing/authorizations/{_require(second, 'id', 'authorization')}"
            f"/capture",
            quiet=True,
        )

    print("\nan authorization reversed before capture")
    reversed_auth = await authorize(
        rec, card_id, REVERSE_MINOR, "YELLOW CAB CO", CATEGORY_TAXI, name=None
    )
    if reversed_auth is not None:
        # Whether `amount` goes to 0 here, and whether the status is `reversed` or
        # `expired`, is what §8.7 rests on.
        await rec.call(
            "POST",
            f"/test_helpers/issuing/authorizations/"
            f"{_require(reversed_auth, 'id', 'authorization')}/reverse",
            name="authorization_reversed",
        )

    print("\nan authorization left pending, so a hold exists")
    await authorize(
        rec,
        card_id,
        PENDING_MINOR,
        "THE ANALYTICAL DINER",
        CATEGORY_RESTAURANT,
        name="authorization_pending",
    )


async def walk_balance_lists(rec: Recorder, card_id: str) -> None:
    """The two lists `get_balance` adds up, and the cursor behaviour it relies on."""
    print("\nthe two lists get_balance reads")
    await rec.call(
        "GET",
        "/issuing/transactions",
        name="transactions_all",
        params={"card": card_id, "limit": 100},
    )
    await rec.call(
        "GET",
        "/issuing/authorizations",
        name="authorizations_pending",
        params={"card": card_id, "status": "pending", "limit": 100},
    )
    # Two pages of one, to pin the cursor behaviour `list_all` depends on.
    page_1 = await rec.call(
        "GET",
        "/issuing/transactions",
        name="transactions_page_1",
        params={"card": card_id, "limit": 1},
    )
    data = page_1.get("data")
    if isinstance(data, list) and data:
        await rec.call(
            "GET",
            "/issuing/transactions",
            name="transactions_page_2",
            params={"card": card_id, "limit": 1, "starting_after": data[-1]["id"]},
        )


async def walk_errors(rec: Recorder) -> None:
    """The error shapes the adapter translates, straight from Stripe."""
    print("\nerror shapes")
    await rec.call("GET", "/issuing/cards/ic_nope", name="error_resource_missing", expect=404)
    # A malformed id rather than an absent one: whether Stripe answers 404 or 400 here
    # decides whether `_is_missing` needs the `resource_missing` code check at all.
    await rec.call("GET", "/issuing/cards/nope", name="error_malformed_id", expect=404)
    await rec.call(
        "POST",
        "/issuing/cards",
        name="error_missing_cardholder",
        form={"currency": CURRENCY, "type": "virtual", "cardholder": "ich_nope"},
        expect=400,
    )


def derive_variants(rec: Recorder) -> None:
    """Card states Stripe will not hand us on demand, mutated from a recorded one.

    Four of the contract tests need cards this walk does not naturally produce: one
    with no spending limit at all, one limited on the wrong interval, one limited to
    zero. Asking Stripe for each would add three cards and three round trips to prove
    something about *our* reading of a limit rather than about their API.

    So they are derived from the recorded `card_activated` — real field names, real
    nullability, real everything, with one documented mutation each. The base is
    recorded; the mutation is stated here and in the fixture README.
    """
    print("\nderived variants (mutated from the recorded card, not fetched)")
    base = json.loads((FIXTURES / "card_activated.json").read_text())

    def variant(name: str, limits: list[dict[str, Any]] | None, note: str) -> None:
        card = json.loads(json.dumps(base))
        controls = dict(card["spending_controls"])
        # `spending_limits` is `[]` for an unlimited card, never absent.
        controls["spending_limits"] = limits if limits is not None else []
        card["spending_controls"] = controls
        rec.write(name, card)
        print(f"  {name:<24} {note}")

    variant(
        "card_with_limit",
        [{"amount": 100_000, "categories": [], "interval": ALL_TIME}],
        "an all_time limit, unfunded",
    )
    variant("card_unlimited", None, "no limits at all — Stripe's way of saying unlimited")
    variant(
        "card_monthly_limit",
        [{"amount": 100_000, "categories": [], "interval": "monthly"}],
        "the wrong interval: resets, so not a balance",
    )
    variant(
        "card_limited_to_zero",
        [{"amount": 0, "categories": [], "interval": ALL_TIME}],
        "zero, which at Stripe really means zero",
    )


async def walk_events(rec: Recorder) -> None:
    """The event feed — real Event envelopes, which is what a webhook delivers.

    This is how the webhook fixtures get verified without a public URL: the body of a
    delivery *is* one of these objects. Each interesting type is written out on its
    own so `parse_webhook` can be tested against the real envelope.
    """
    print("\nevent feed")
    feed = await rec.call("GET", "/events", params={"limit": 100})
    events = feed.get("data")
    if not isinstance(events, list):
        print("  !! no event list to split up")
        return

    wanted = {
        "issuing_authorization.created": "event_authorization_created",
        "issuing_authorization.updated": "event_authorization_updated",
        "issuing_authorization.request": "event_authorization_request",
        "issuing_transaction.created": "event_transaction_created",
        "issuing_card.created": "event_card_created",
        "issuing_card.updated": "event_card_updated",
        "issuing_cardholder.created": "event_cardholder_created",
    }
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        name = wanted.get(str(kind))
        if name and kind not in seen:
            seen.add(str(kind))
            rec.write(name, event)
            print(f"  wrote {name} ({kind})")
    missing = set(wanted) - seen
    if missing:
        print(f"  !! never saw: {sorted(missing)}")
        print("     (the hand-authored fixture for each stays in place, unverified)")

    # A census rather than the feed itself. The raw feed is ~half a megabyte of this
    # account's recent history — noise, and no test reads it. What is worth keeping is
    # the *vocabulary*: which event types Stripe really sends. `test_stripe_webhooks.py`
    # checks every type this adapter claims to map against it, which is what would
    # catch a typo in a hand-written event name.
    census: dict[str, int] = {}
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            census[event["type"]] = census.get(event["type"], 0) + 1
    rec.write("event_type_census", dict(sorted(census.items())))
    print(f"  wrote event_type_census ({len(census)} distinct types over {len(events)} events)")


def _require(payload: dict[str, Any], field: str, what: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"stripe did not return a {what} {field}: {payload!r}")
    return value


def _check_marker_survived(card: dict[str, Any]) -> None:
    """Whether Stripe merges metadata or replaces it — the §8.3 assumption.

    The freeze above sent no metadata at all. If the marker is still there, metadata
    is only touched when the request carries it, and `activate_card`'s extra read
    exists for nothing. If it is gone, the read is load-bearing and this line is how
    we found out.
    """
    metadata = card.get("metadata")
    keys = sorted(metadata) if isinstance(metadata, dict) else []
    if "stablecard_activated_at" in keys:
        print("  ok: metadata survived a body that did not mention it (§8.3 confirmed)")
    else:
        print(f"  !! METADATA WAS DROPPED by a status-only update — §8.3 is wrong. keys={keys}")


# ------------------------------------------------------------------- entry ----


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="walk but write nothing")
    parser.add_argument("--preflight", action="store_true", help="check the account and stop")
    args = parser.parse_args()

    settings = get_stripe_issuing_settings()
    if not settings.api_key:
        print("STRIPE_ISSUING_API_KEY is not set (see .env.example)", file=sys.stderr)
        return 2
    if "_live_" in settings.api_key:
        print("that is a live key; this project is sandbox-only (SPEC.md §2)", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Accept": "application/json",
        },
    ) as client:
        rec = Recorder(client, dry_run=args.dry_run or args.preflight)
        funded = await preflight(rec)
        if args.preflight:
            print("\npreflight only; nothing walked")
            return 0
        await walk(rec, funded=funded)

    print(f"\n{len(rec.written)} fixtures {'walked' if args.dry_run else 'written'}")
    if not args.dry_run:
        print(f"  -> {FIXTURES}")
        print("  Now run the suite. A failure here is the adapter being wrong, not the")
        print("  fixture: recorded shapes are evidence, authored ones were a reading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
