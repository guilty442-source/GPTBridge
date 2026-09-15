from __future__ import annotations

import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .analytics_helpers import utc_now, utc_text

FetchJson = Callable[[str], Any]

POSITIVE_WORDS = {"beat", "growth", "raise", "upgrade", "profit", "surge", "record", "成長", "上修", "獲利", "創高", "優於"}
NEGATIVE_WORDS = {"miss", "cut", "downgrade", "loss", "fall", "risk", "fraud", "下修", "虧損", "衰退", "風險", "裁員"}

def sentiment_score(text: str) -> tuple[float, float]:
    lowered = str(text or "").lower()
    positive = sum(1 for word in POSITIVE_WORDS if word in lowered)
    negative = sum(1 for word in NEGATIVE_WORDS if word in lowered)
    total = positive + negative
    return ((positive - negative) / total if total else 0.0, min(1.0, 0.35 + total * 0.12))


def _open_json(
    open_url: Callable[..., Any],
    url: str,
    timeout_seconds: float,
) -> Any:
    with open_url(url, timeout=max(1.0, min(30.0, timeout_seconds))) as response:
        status = int(getattr(response, "status", 200) or 200)
        if status == 429 or 500 <= status <= 599:
            raise urllib.error.HTTPError(
                url,
                status,
                f"transient HTTP status {status}",
                getattr(response, "headers", None),
                None,
            )
        if status >= 400:
            raise urllib.error.HTTPError(
                url,
                status,
                f"HTTP status {status}",
                getattr(response, "headers", None),
                None,
            )
        return json.loads(
            response.read().decode("utf-8", errors="replace")
        )


def _http_retry_delay(
    exc: urllib.error.HTTPError,
    base_delay_seconds: float,
    attempt: int,
) -> float:
    retry_after = 0.0
    try:
        retry_after = float(exc.headers.get("Retry-After") or 0)
    except (AttributeError, TypeError, ValueError):
        retry_after = 0.0
    return min(
        2.0,
        max(
            retry_after,
            max(0.0, base_delay_seconds) * (2**attempt),
        ),
    )


def fetch_json_with_retry(
    url: str,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 3,
    timeout_seconds: float = 15,
    base_delay_seconds: float = 0.25,
) -> Any:
    """Decode injected JSON data without granting this tool direct network access."""
    if opener is None:
        raise PermissionError(
            "AI investment manager has no network access; Xingcheng must inject market data."
        )
    open_url = opener
    wait = sleep or time.sleep
    maximum_attempts = max(1, min(5, int(attempts)))
    last_error: Exception | None = None
    for attempt in range(maximum_attempts):
        try:
            return _open_json(open_url, url, timeout_seconds)
        except urllib.error.HTTPError as exc:
            last_error = exc
            transient = exc.code == 429 or 500 <= exc.code <= 599
            if not transient or attempt + 1 >= maximum_attempts:
                raise
            wait(_http_retry_delay(exc, base_delay_seconds, attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= maximum_attempts:
                raise
            wait(min(2.0, max(0.0, base_delay_seconds) * (2**attempt)))
    if last_error is not None:
        raise last_error
    raise RuntimeError("public JSON fetch failed")


def _default_fetch_json(url: str) -> Any:
    raise PermissionError(
        "AI investment manager has no network access; Xingcheng must provide market data."
    )


def yahoo_symbol(symbol: str, market: str) -> str:
    normalized = symbol.strip().upper()
    if market.upper() == "TW" and not normalized.endswith((".TW", ".TWO")):
        return f"{normalized}.TW"
    if market.upper() == "HK" and not normalized.endswith(".HK"):
        return f"{int(normalized):04d}.HK" if normalized.isdigit() else f"{normalized}.HK"
    if market.upper() == "CRYPTO" and "-" not in normalized:
        return f"{normalized}-USD"
    return normalized


MARKET_SESSION_DEFINITIONS: dict[str, dict[str, Any]] = {
    "TW": {
        "timezone": "Asia/Taipei",
        "sessions": ((9 * 60, 13 * 60 + 30),),
        "schedule": "09:00-13:30",
    },
    "US": {
        "timezone": "America/New_York",
        "sessions": ((9 * 60 + 30, 16 * 60),),
        "schedule": "09:30-16:00",
    },
    "HK": {
        "timezone": "Asia/Hong_Kong",
        "sessions": ((9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60)),
        "schedule": "09:30-12:00 / 13:00-16:00",
    },
}


def _nth_sunday(year: int, month: int, occurrence: int) -> int:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return 1 + (6 - first.weekday()) % 7 + (occurrence - 1) * 7


def _local_market_time(observed: datetime, timezone_name: str) -> datetime:
    if timezone_name in {"Asia/Taipei", "Asia/Hong_Kong"}:
        return observed.astimezone(timezone(timedelta(hours=8)))
    year = observed.year
    dst_start = datetime(
        year,
        3,
        _nth_sunday(year, 3, 2),
        7,
        tzinfo=timezone.utc,
    )
    dst_end = datetime(
        year,
        11,
        _nth_sunday(year, 11, 1),
        6,
        tzinfo=timezone.utc,
    )
    eastern_offset = -4 if dst_start <= observed < dst_end else -5
    return observed.astimezone(timezone(timedelta(hours=eastern_offset)))


def market_session_status(now: datetime | None = None) -> dict[str, Any]:
    observed = now or utc_now()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    markets: dict[str, dict[str, Any]] = {}
    open_markets: list[str] = []
    for market, definition in MARKET_SESSION_DEFINITIONS.items():
        local_time = _local_market_time(observed, str(definition["timezone"]))
        minute = local_time.hour * 60 + local_time.minute
        weekday = local_time.weekday() < 5
        is_open = weekday and any(
            start <= minute < end for start, end in definition["sessions"]
        )
        if is_open:
            open_markets.append(market)
        markets[market] = {
            "market": market,
            "is_open": is_open,
            "timezone": definition["timezone"],
            "schedule": definition["schedule"],
            "local_time": local_time.isoformat(),
            "weekday": weekday,
        }
    return {
        "as_of": utc_text(observed),
        "open_markets": open_markets,
        "markets": markets,
    }
