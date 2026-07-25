"""The mock's configuration, owned by the mock — see `lithic/config.py` for why.

There is almost nothing here, which is the point: this provider is a simulator in
this process, so it has no credentials to hold and no base URL to point at. The
one real knob is the webhook receiving window, and it belongs to whoever owns the
signing scheme.

No signing key appears here either, and could not: the scheme is asymmetric, so
the adapter verifies with a published public key and holds no secret at all. The
simulator derives the private half from a seed in `signing.py`, where it is worth
nothing to anyone — it authenticates a Python object in this process to itself.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.issuers.gnosis_pay_mock.signing import DEFAULT_TOLERANCE_SECONDS

__all__ = ["GnosisPayMockSettings", "get_gnosis_pay_mock_settings"]


class GnosisPayMockSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="GNOSIS_PAY_MOCK_",
        extra="ignore",
        case_sensitive=False,
    )

    #: Rejection window for a delivery's signature timestamp, in either direction.
    signature_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS


@lru_cache
def get_gnosis_pay_mock_settings() -> GnosisPayMockSettings:
    return GnosisPayMockSettings()
