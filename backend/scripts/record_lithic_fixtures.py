"""Record the Lithic contract-test fixtures from the live sandbox.

    python scripts/record_lithic_fixtures.py            # write tests/fixtures/lithic/
    python scripts/record_lithic_fixtures.py --dry-run  # walk, print, write nothing

Needs `LITHIC_API_KEY`. **The test suite never runs this** — it replays what this
wrote (SPEC.md §10: tests never call a live sandbox API). Re-run it when Lithic
changes a payload, and the diff is the change.

It deliberately talks to Lithic with plain `httpx` and literal paths instead of
going through `app.issuers.lithic`. Fixtures recorded *by* the adapter could only
ever confirm the adapter agrees with itself; recorded independently, they are
evidence about the provider, and the contract tests compare the two.

What it walks, in order: an account holder (KYC_EXEMPT), a virtual card, the same
create replayed under one idempotency key and then a conflicting one, the card read
back, paused, reopened, funded by raising its spend limit, an authorization, its
clearing, a second authorization voided, a refund, an over-limit decline, the
transaction list in two pages, the event feed, four error shapes, and finally the
card closed and a change refused on it.

PAN and CVV are replaced with obvious synthetic values before anything is written:
sandbox card numbers are not credentials, but card-number-shaped material has no
business in a tracked file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "lithic"

REDACTED_PAN = "4111111111111111"
REDACTED_CVV = "123"

#: How long to wait for the sandbox to reflect an asynchronous simulation. Polling
#: faster than this earns a 429 from the sandbox, which is how the first run of this
#: script lost a void.
SETTLE_TIMEOUT_SECONDS = 40.0
SETTLE_POLL_SECONDS = 3.0

#: Waits before re-sending a request the sandbox rate-limited.
RATE_LIMIT_BACKOFF_SECONDS = (2.0, 5.0, 15.0)


class Recorder:
    """Calls Lithic and writes each response verbatim, under a stable name."""

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
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect: int = 200,
        quiet: bool = False,
    ) -> dict[str, Any]:
        response = await self.client.request(method, path, json=json_body, headers=headers)
        for pause in RATE_LIMIT_BACKOFF_SECONDS:
            if response.status_code != 429:
                break
            print(f"  [429] {method} {path} — rate limited, waiting {pause}s")
            await asyncio.sleep(pause)
            response = await self.client.request(method, path, json=json_body, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {"_non_json_body": response.text}
        if not quiet:
            flag = "" if response.status_code == expect else f"  (expected {expect})"
            print(f"  [{response.status_code}] {method} {path}{flag}")
        if name:
            self.write(name, payload)
        return dict(payload) if isinstance(payload, dict) else {"_root": payload}

    def write(self, name: str, payload: Any) -> None:
        self.written.append(name)
        if self.dry_run:
            return
        FIXTURES.mkdir(parents=True, exist_ok=True)
        text = json.dumps(redact(payload), indent=2, sort_keys=True) + "\n"
        (FIXTURES / f"{name}.json").write_text(text)


def redact(payload: Any) -> Any:
    """Strip card-number material, recursively, leaving everything else verbatim."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "pan" and isinstance(value, str):
                out[key] = REDACTED_PAN
            elif key == "cvv" and isinstance(value, str):
                out[key] = REDACTED_CVV
            else:
                out[key] = redact(value)
        return out
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


async def wait_for_status(rec: Recorder, token: str, wanted: str) -> dict[str, Any]:
    """Poll one transaction until the sandbox reports `wanted`.

    Clearing and void are asynchronous there — a fixture captured too early records
    a state the adapter would never be asked to interpret.
    """
    waited = 0.0
    transaction: dict[str, Any] = {}
    while waited < SETTLE_TIMEOUT_SECONDS:
        # A freshly simulated authorization is briefly a 404 here, so the first
        # attempt is expected to miss.
        transaction = await rec.call("GET", f"/transactions/{token}", expect=-1, quiet=True)
        if transaction.get("status") == wanted:
            return transaction
        await asyncio.sleep(SETTLE_POLL_SECONDS)
        waited += SETTLE_POLL_SECONDS
    raise SystemExit(
        f"transaction {token} never reached {wanted} (last: {transaction.get('status')!r})"
    )


async def walk(rec: Recorder) -> None:
    print("\naccount holder (KYC_EXEMPT)")
    holder = await rec.call(
        "POST",
        "/account_holders",
        name="account_holder_created",
        json_body={
            "workflow": "KYC_EXEMPT",
            "kyc_exemption_type": "PREPAID_CARD_USER",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.test",
            "phone_number": "+15555550123",
            "address": {
                "address1": "1 Analytical Engine Way",
                "city": "New York",
                "state": "NY",
                "postal_code": "10128",
                "country": "USA",
            },
            "external_id": f"stablecard-fixture-{uuid.uuid4().hex[:8]}",
        },
    )
    account_token = holder["account_token"]

    print("\nvirtual card")
    idempotency_key = str(uuid.uuid4())
    create_body = {
        "type": "VIRTUAL",
        "account_token": account_token,
        "state": "OPEN",
        "memo": "StableCard Rail demo card",
        "spend_limit": 50_000,
        "spend_limit_duration": "FOREVER",
    }
    card = await rec.call(
        "POST",
        "/cards",
        name="card_created",
        json_body=create_body,
        headers={"Idempotency-Key": idempotency_key},
    )
    card_token, pan = card["token"], card["pan"]

    # The same key with the same body returns the same card rather than a second one.
    await rec.call(
        "POST",
        "/cards",
        name="card_created_replayed",
        json_body=create_body,
        headers={"Idempotency-Key": idempotency_key},
    )
    # The same key with a different body is refused, which is the interesting half.
    await rec.call(
        "POST",
        "/cards",
        name="error_idempotency_key_reused",
        json_body={**create_body, "memo": "something else"},
        headers={"Idempotency-Key": idempotency_key},
        expect=422,
    )

    await rec.call("GET", f"/cards/{card_token}", name="card_read_back")

    print("\nlifecycle")
    await rec.call(
        "PATCH", f"/cards/{card_token}", name="card_paused", json_body={"state": "PAUSED"}
    )
    await rec.call(
        "PATCH", f"/cards/{card_token}", name="card_reopened", json_body={"state": "OPEN"}
    )

    print("\nfunding (spend limit raised, funding ref recorded in the memo)")
    await rec.call(
        "PATCH",
        f"/cards/{card_token}",
        name="card_funded",
        json_body={
            "spend_limit": 75_000,
            "spend_limit_duration": "FOREVER",
            "memo": "StableCard Rail demo card [fund:fi_0001]",
        },
    )

    print("\nsimulated authorization, then its clearing")
    authorized = await rec.call(
        "POST",
        "/simulate/authorize",
        name="simulate_authorize_response",
        json_body={
            "pan": pan,
            "amount": 1_234,
            "descriptor": "COFFEE BAR",
            "merchant_acceptor_id": "OODKZAPJVN4YS7O",
        },
        expect=201,
    )
    settled_token = authorized["token"]
    rec.write("transaction_pending", await wait_for_status(rec, settled_token, "PENDING"))
    await rec.call(
        "POST",
        "/simulate/clearing",
        name="simulate_clearing_response",
        json_body={"token": settled_token, "amount": 1_234},
        expect=201,
    )
    rec.write("transaction_settled", await wait_for_status(rec, settled_token, "SETTLED"))

    print("\nsecond authorization, voided")
    voided = await rec.call(
        "POST",
        "/simulate/authorize",
        json_body={
            "pan": pan,
            "amount": 500,
            "descriptor": "VOID ME",
            "merchant_acceptor_id": "OODKZAPJVN4YS7O",
        },
        expect=201,
    )
    await rec.call(
        "POST", "/simulate/void", json_body={"token": voided["token"], "amount": 500}, expect=201
    )
    rec.write("transaction_voided", await wait_for_status(rec, voided["token"], "VOIDED"))

    print("\nrefund")
    returned = await rec.call(
        "POST",
        "/simulate/return",
        json_body={"pan": pan, "amount": 250, "descriptor": "REFUND SHOP"},
        expect=201,
    )
    rec.write("transaction_returned", await wait_for_status(rec, returned["token"], "SETTLED"))

    print("\nover-limit decline")
    declined = await rec.call(
        "POST",
        "/simulate/authorize",
        json_body={
            "pan": pan,
            "amount": 10_000_000,
            "descriptor": "OVER LIMIT",
            "merchant_acceptor_id": "OODKZAPJVN4YS7O",
        },
        expect=201,
    )
    rec.write("transaction_declined", await wait_for_status(rec, declined["token"], "DECLINED"))

    print("\ntransaction list, paged (balance is computed from all of it)")
    page = await rec.call(
        "GET", f"/transactions?card_token={card_token}&page_size=100", name="transactions_all"
    )
    first = await rec.call(
        "GET", f"/transactions?card_token={card_token}&page_size=2", name="transactions_page_1"
    )
    cursor = first["data"][-1]["token"]
    await rec.call(
        "GET",
        f"/transactions?card_token={card_token}&page_size=2&starting_after={cursor}",
        name="transactions_page_2",
    )
    print(f"  {len(page['data'])} transactions for this card")

    print("\nevent feed (each `payload` is exactly what a webhook delivers)")
    await rec.call("GET", "/events?page_size=50", name="events_all")

    print("\nerror shapes")
    await rec.call(
        "GET",
        "/cards/00000000-0000-4000-8000-000000000999",
        name="error_card_not_found",
        expect=404,
    )
    await rec.call("GET", "/cards/not-a-uuid", name="error_invalid_uuid", expect=400)

    print("\nclosing the card, then a change refused on it")
    await rec.call(
        "PATCH", f"/cards/{card_token}", name="card_closed", json_body={"state": "CLOSED"}
    )
    await rec.call(
        "PATCH",
        f"/cards/{card_token}",
        name="error_card_closed",
        json_body={"state": "OPEN"},
        expect=405,
    )


async def record_unauthorized(base_url: str, seconds: float, rec: Recorder) -> None:
    """The 401 shape, which needs a client that is deliberately not authenticated."""
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": "not-a-real-key"}, timeout=seconds
    ) as client:
        response = await client.get("/cards", params={"page_size": 1})
        print(f"  [{response.status_code}] GET /cards (bad key)")
        rec.write("error_unauthorized", response.json())


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="walk the API, write no files")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.lithic_api_key:
        print("LITHIC_API_KEY is not set — see .env.example", file=sys.stderr)
        return 2
    if "sandbox" not in settings.lithic_api_base_url:
        print(
            f"refusing to record against {settings.lithic_api_base_url!r}: "
            f"this script creates cards and simulates transactions",
            file=sys.stderr,
        )
        return 2

    print(f"recording against {settings.lithic_api_base_url}")
    async with httpx.AsyncClient(
        base_url=settings.lithic_api_base_url,
        headers={"Authorization": settings.lithic_api_key, "Accept": "application/json"},
        timeout=settings.lithic_request_timeout_seconds,
    ) as client:
        rec = Recorder(client, dry_run=args.dry_run)
        await walk(rec)
        print("\nunauthenticated request")
        await record_unauthorized(
            settings.lithic_api_base_url, settings.lithic_request_timeout_seconds, rec
        )

    where = "(dry run, nothing written)" if args.dry_run else str(FIXTURES)
    print(f"\n{len(rec.written)} fixtures {where}")
    for name in rec.written:
        print(f"  {name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
