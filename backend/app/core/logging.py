"""Logging setup."""

from __future__ import annotations

import logging

from app.core.config import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    """Idempotent logging setup; safe to call from every app factory."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    _CONFIGURED = True
