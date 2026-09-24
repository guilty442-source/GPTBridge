"""TradingCalendar — per-market sessions, holidays, DST via zoneinfo.

- TW: TWSE/TPEx regular session 09:00–13:30 Asia/Taipei, Mon–Fri minus
  holiday list.
- US: NYSE/NASDAQ regular 09:30–16:00 America/New_York; pre/post
  sessions declared; DST handled by zoneinfo — US open time is NEVER
  hardcoded as a fixed Taiwan clock time.
- All stored timestamps are timezone-aware UTC; UI converts to Taiwan
  time for display at the presentation layer.
- Holiday/session overrides load from ``runtime/state/market-calendar.json``
  — an updatable, auditable source (operator-provided until an official
  calendar feed is verified).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ_TW = ZoneInfo("Asia/Taipei")
TZ_US = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SessionWindow:
    """A named session as local (open, close) wall-clock times."""

    name: str
    open_local: time
    close_local: time


# Regular sessions — local times, DST resolved by the market timezone.
_SESSIONS: dict[str, tuple[SessionWindow, ...]] = {
    "tw": (
        SessionWindow("regular", time(9, 0), time(13, 30)),
    ),
    "us": (
        SessionWindow("pre", time(4, 0), time(9, 30)),
        SessionWindow("regular", time(9, 30), time(16, 0)),
        SessionWindow("post", time(16, 0), time(20, 0)),
    ),
    "fund": (
        SessionWindow("regular", time(0, 0), time(23, 59)),
    ),
}

_MARKET_TZ = {"tw": TZ_TW, "us": TZ_US, "fund": TZ_TW}

# Built-in known holidays (baseline; file overrides extend/replace).
_BUILTIN_HOLIDAYS: dict[str, set[date]] = {
    "tw": {
        date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17),
        date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20),
        date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1),
        date(2026, 6, 19), date(2026, 9, 25), date(2026, 9, 28),
        date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 26),
    },
    "us": {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
        date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
        date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
        date(2026, 12, 25),
    },
    "fund": set(),
}


@dataclass
class CalendarUpdate:
    """One calendar correction — auditable."""

    market: str
    day: date
    closed: bool
    reason: str
    source: str = "operator"
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TradingCalendar:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._path = (
            Path(state_dir) / "market-calendar.json" if state_dir else None
        )
        self._overrides: dict[str, dict[date, CalendarUpdate]] = {
            m: {} for m in _SESSIONS
        }
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                upd = CalendarUpdate(
                    market=row["market"],
                    day=date.fromisoformat(row["day"]),
                    closed=bool(row["closed"]),
                    reason=str(row.get("reason") or ""),
                    source=str(row.get("source") or "operator"),
                )
            except (KeyError, ValueError):
                continue
            self._overrides.setdefault(upd.market, {})[upd.day] = upd

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "market": u.market,
                "day": u.day.isoformat(),
                "closed": u.closed,
                "reason": u.reason,
                "source": u.source,
                "updated_at": u.updated_at.isoformat(),
            }
            for updates in self._overrides.values()
            for u in updates.values()
        ]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    def update_day(
        self, market: str, day: date, closed: bool, reason: str, source: str = "operator"
    ) -> CalendarUpdate:
        """Apply a calendar correction (temporary close / reopen)."""
        if market not in _SESSIONS:
            raise ValueError(f"unknown market {market!r}")
        upd = CalendarUpdate(
            market=market, day=day, closed=closed, reason=reason, source=source
        )
        self._overrides.setdefault(market, {})[day] = upd
        self._persist()
        return upd

    # ------------------------------------------------------------------
    def is_trading_day(self, market: str, day: date) -> bool:
        if market not in _SESSIONS:
            return False
        override = self._overrides.get(market, {}).get(day)
        if override is not None:
            return not override.closed
        if day.weekday() >= 5:
            return False
        return day not in _BUILTIN_HOLIDAYS.get(market, set())

    def session_for(self, market: str, at: datetime) -> str:
        """Session name at `at` — 'closed' when outside all sessions."""
        tz = _MARKET_TZ.get(market)
        sessions = _SESSIONS.get(market)
        if tz is None or sessions is None:
            return "closed"
        local = at.astimezone(tz)
        if not self.is_trading_day(market, local.date()):
            return "closed"
        for window in sessions:
            if window.open_local <= local.time() < window.close_local:
                return window.name
        return "closed"

    def is_open(self, market: str, at: datetime) -> bool:
        return self.session_for(market, at) != "closed"

    def local_now(self, market: str) -> datetime:
        """Current wall-clock time in the market's own timezone."""
        tz = _MARKET_TZ.get(market)
        return datetime.now(tz or timezone.utc)

    def session_bounds_utc(
        self, market: str, day: date, session: str = "regular"
    ) -> tuple[datetime, datetime] | None:
        """UTC bounds of a session on a date — DST-correct via zoneinfo."""
        tz = _MARKET_TZ.get(market)
        if tz is None:
            return None
        for window in _SESSIONS.get(market, ()):
            if window.name == session:
                open_dt = datetime.combine(day, window.open_local, tzinfo=tz)
                close_dt = datetime.combine(day, window.close_local, tzinfo=tz)
                return (
                    open_dt.astimezone(timezone.utc),
                    close_dt.astimezone(timezone.utc),
                )
        return None

    def next_trading_day(self, market: str, after: date) -> date:
        day = after + timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(market, day):
                return day
            day += timedelta(days=1)
        raise ValueError(f"no trading day found for {market}")

    def trading_days(self, market: str, start: date, end: date) -> list[date]:
        days: list[date] = []
        day = start
        while day <= end:
            if self.is_trading_day(market, day):
                days.append(day)
            day += timedelta(days=1)
        return days
