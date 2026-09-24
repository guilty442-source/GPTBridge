"""Point-in-time data gate — the look-ahead firewall.

Every data row is gated on `available_at` (when the information was
actually usable). Daily candles confirm at session close + publish lag;
NAV rows only exist after publication; corporate actions apply only to
bars after their effective date. Strategy code receives slices truncated
at `as_of` — it physically cannot see the future.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


class PITViolation(Exception):
    pass


def bar_available_at(candle_end: datetime,
                     publish_lag: timedelta = timedelta(0)) -> datetime:
    """A bar becomes usable at close + lag (default: at close)."""
    return candle_end + publish_lag


def slice_bars_at(bars: Iterable[Any], as_of: datetime,
                  publish_lag: timedelta = timedelta(0)) -> list[Any]:
    """Bars visible to the strategy at decision time `as_of`."""
    return [
        b for b in bars
        if bar_available_at(_dt(b.candle_end), publish_lag) <= as_of
    ]


def nav_available_at(nav_date, publish_delay_days: int = 1) -> datetime:
    """Fund NAV is usable only after publication (default next day)."""
    base = datetime.combine(nav_date, datetime.min.time(),
                            tzinfo=timezone.utc)
    return base + timedelta(days=publish_delay_days)


def gate_nav(rows: Iterable[Any], as_of: datetime,
             publish_delay_days: int = 1) -> list[Any]:
    return [r for r in rows
            if nav_available_at(r.nav_date, publish_delay_days) <= as_of]


def gate_corporate(actions: Iterable[Any], as_of: datetime) -> list[Any]:
    """Corporate actions visible only after effective_date."""
    out = []
    for a in actions:
        eff = getattr(a, "effective_date", None) or a.get("effective_date")
        eff_dt = _dt(eff) or _dt(str(eff))
        if eff_dt and eff_dt <= as_of:
            out.append(a)
    return out


def assert_no_future(rows: Iterable[Any], as_of: datetime,
                     attr: str = "available_at") -> None:
    """Hard check: raise if any row's availability exceeds as_of."""
    for r in rows:
        av = getattr(r, attr, None)
        if av is None and isinstance(r, dict):
            av = r.get(attr)
        if av is not None and _dt(av) and _dt(av) > as_of:
            raise PITViolation(f"future data used at {as_of.isoformat()}")


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
