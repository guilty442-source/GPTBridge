"""Shared, side-effect-free helpers for sovereign implementations."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    return contextlib.suppress(*exceptions)


__all__ = ["_iso_now", "_suppress"]
