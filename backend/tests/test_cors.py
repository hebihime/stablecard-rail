"""Cross-origin access, for the web build of the phase-8 client (SPEC.md §9).

Nothing needed this before: every caller so far has been a provider posting a
webhook, a script, or the test suite, and none of them is a browser. The app is one
codebase built for both native and web (docs/ARCHITECTURE.md §12.6), and the web
half runs on an origin of its own.

**CORS is load-bearing here in a way it usually is not.** This API has no
authentication at all (§11.6), so on a developer's machine the only thing between
`evil.example` in an open tab and a backend on `localhost:8000` is whether the
browser will hand over the response. That is why `*` is refused rather than merely
discouraged: with no auth, a wildcard means any page anyone visits can drive this
service.

The last test here pins something uncomfortable rather than assuming it away.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings

ALLOWED = "http://localhost:8081"
FOREIGN = "http://evil.example"


def settings(**overrides: Any) -> Settings:
    # `_env_file=None` or this reads jp's real .env and passes or fails depending on
    # what happens to be configured (see tests/test_stripe_config.py).
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_expo_dev_server_is_allowed_out_of_the_box() -> None:
    # So `npx expo start --web` against a local backend works with no configuration.
    # Both spellings: a browser sends whichever the address bar holds, and
    # `localhost` and `127.0.0.1` are different origins to the same-origin policy.
    assert set(settings().cors_allowed_origins) == {
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    }


def test_a_wildcard_origin_is_refused() -> None:
    """Not a style preference — this API has no auth.

    With a wildcard, any page in any tab could read a cardholder's balance, mint a
    reveal token and answer a 3DS challenge on a machine running the demo. A
    deployed origin is one env var; a wildcard is a different thing entirely.
    """
    with pytest.raises(ValidationError, match="wildcard"):
        settings(cors_allowed_origins=["*"])


def test_a_wildcard_hidden_among_real_origins_is_also_refused() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        settings(cors_allowed_origins=[ALLOWED, "*"])


async def test_an_allowed_origin_gets_the_header(client: httpx.AsyncClient) -> None:
    response = await client.get("/providers", headers={"Origin": ALLOWED})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED


async def test_a_foreign_origin_is_answered_but_not_readable(
    client: httpx.AsyncClient,
) -> None:
    """The server still answers; the browser is what refuses to hand it over.

    Worth stating in a test, because "CORS blocked it" reads as though the request
    did not happen. It did — this is not authorization, and it is not a substitute
    for the auth this demo does not have.
    """
    response = await client.get("/providers", headers={"Origin": FOREIGN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_a_preflight_from_an_allowed_origin_permits_the_real_request(
    client: httpx.AsyncClient,
) -> None:
    # The app POSTs JSON, which is not a "simple" request, so every mutating call
    # it makes is preceded by one of these.
    response = await client.request(
        "OPTIONS",
        "/reveal",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in response.headers["access-control-allow-methods"]


async def test_a_preflight_from_a_foreign_origin_is_not_granted(
    client: httpx.AsyncClient,
) -> None:
    response = await client.request(
        "OPTIONS",
        "/reveal",
        headers={"Origin": FOREIGN, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


async def test_the_websocket_is_not_protected_by_cors_and_that_is_not_fixable_here() -> None:
    """The uncomfortable one, pinned rather than assumed.

    `CORSMiddleware` handles `scope["type"] == "http"` and passes everything else
    straight through, so `/ws/otp` accepts a connection from any origin no matter
    what `CORS_ALLOWED_ORIGINS` says. That is not a Starlette bug: the same-origin
    policy has never applied to WebSockets, which is why every real deployment
    checks `Origin` inside the handshake instead.

    Not done here, because the honest fix is the auth this demo does not have
    (docs/ARCHITECTURE.md §12.7) — an origin check would look like a control and
    stop nothing that matters, since anything not a browser sends whatever origin
    it likes. So the property is recorded, and this test fails the day someone
    assumes otherwise.
    """
    from starlette.middleware.cors import CORSMiddleware

    reached = False

    async def inner(scope: object, receive: object, send: object) -> None:
        nonlocal reached
        reached = True

    middleware = CORSMiddleware(inner, allow_origins=[ALLOWED])
    await middleware(
        {"type": "websocket", "path": "/ws/otp", "headers": [(b"origin", FOREIGN.encode())]},
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    assert reached, "CORS does not gate WebSockets; the OTP socket is origin-agnostic"


def test_credentials_are_not_allowed_across_origins() -> None:
    """Nothing to send, so nothing is permitted to be sent.

    This service has no cookies and no session. Turning credentials on would be
    inviting a future auth mechanism to be usable cross-origin by default, which is
    the wrong default to inherit.
    """
    from app.main import create_app

    middleware = [m for m in create_app().user_middleware if "CORS" in str(m.cls)]
    assert len(middleware) == 1
    assert middleware[0].kwargs["allow_credentials"] is False
