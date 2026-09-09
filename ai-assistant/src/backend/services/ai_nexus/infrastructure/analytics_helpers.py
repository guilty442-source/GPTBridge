from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def normalized_probability(value: Any, default: float = 0.5) -> float:
    """Accept legacy percentages or canonical 0..1 probabilities."""
    parsed = number(value, default)
    if parsed > 1:
        parsed /= 100
    return max(0.0, min(1.0, parsed))


def rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalized_profile(value: Any) -> str:
    profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "default").strip())
    return profile.strip(".-")[:64] or "default"



__all__ = ['utc_now', 'utc_text', 'parse_datetime', 'number', 'normalized_probability', 'rounded', '_json', '_normalized_profile']
