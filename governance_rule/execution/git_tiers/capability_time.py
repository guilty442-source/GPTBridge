"""UTC time helpers shared by capability issuance, ledger and verification."""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["as_utc", "parse_time", "utc_now_iso"]


def utc_now_iso(moment: datetime | None = None) -> str:
    stamp = moment or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def as_utc(value: datetime | float | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        return parse_time(value)
    return value.astimezone(timezone.utc)


def parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return parsed.replace(
        tzinfo=parsed.tzinfo or timezone.utc
    ).astimezone(timezone.utc)
