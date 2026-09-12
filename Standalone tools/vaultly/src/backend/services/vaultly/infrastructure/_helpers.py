from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
