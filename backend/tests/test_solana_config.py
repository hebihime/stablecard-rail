"""Solana's settings belong to `chain/`, under their own prefix.

Every test passes `_env_file=None`. Without it these read the developer's real
`.env` and start passing or failing depending on what happens to be configured —
a lesson from phase 4 (see `tests/test_stripe_config.py`). mypy cannot see that
argument on a `BaseSettings` subclass, hence the one ignore per construction.
"""

from __future__ import annotations

import pytest

from app.chain.config import USDC_DEVNET_MINT, SolanaSettings, get_solana_settings


def test_it_defaults_to_devnet(monkeypatch: pytest.MonkeyPatch) -> None:
    # A misconfigured environment must not be able to reach mainnet by omission —
    # the same reason Lithic's base URL defaults to the sandbox.
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)

    settings = SolanaSettings(_env_file=None)  # type: ignore[call-arg]

    assert "devnet" in settings.rpc_url
    assert settings.usdc_mint == USDC_DEVNET_MINT
    assert settings.usdc_decimals == 6


def test_every_field_answers_to_the_solana_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLANA_RPC_URL", "http://127.0.0.1:8899")
    monkeypatch.setenv("SOLANA_USDC_MINT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
    monkeypatch.setenv("SOLANA_USDC_DECIMALS", "9")
    monkeypatch.setenv("SOLANA_PAGE_LIMIT", "100")
    monkeypatch.setenv("SOLANA_REQUEST_TIMEOUT_SECONDS", "5.5")

    settings = SolanaSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.rpc_url == "http://127.0.0.1:8899"
    assert settings.usdc_mint == "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    assert settings.usdc_decimals == 9
    assert settings.page_limit == 100
    assert settings.request_timeout_seconds == 5.5


def test_no_unprefixed_variable_reaches_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # `RPC_URL` belongs to nobody. Reading it here is how two components end up
    # sharing a variable neither of them documented.
    monkeypatch.setenv("RPC_URL", "http://elsewhere")

    assert "devnet" in SolanaSettings(_env_file=None).rpc_url  # type: ignore[call-arg]


def test_the_accessor_is_memoized() -> None:
    assert get_solana_settings() is get_solana_settings()
