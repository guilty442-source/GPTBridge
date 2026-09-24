"""InvestmentAnalysisScheduler — calendar-driven, lazy, non-duplicating.

Slots are keyed on the trading calendar: an analysis slot only fires
when its market has a session and fresh data exists. A slot that already
ran for the current market date does not re-run (no duplicate inference).
The scheduler itself does not run continuously — callers invoke
``due_slots``/``mark_ran`` from governed triggers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..market.calendar import TradingCalendar
from .contracts import AnalysisSchedule

_SLOTS: list[tuple[str, str]] = [
    ("tw", "pre_open"), ("tw", "intraday"), ("tw", "post_close"),
    ("us", "pre_open"), ("us", "intraday"), ("us", "post_close"),
    ("fund", "nav_update"),
    ("portfolio", "daily"), ("portfolio", "weekly"), ("portfolio", "monthly"),
]


class InvestmentAnalysisScheduler:
    def __init__(self, state_dir: Path,
                 calendar: TradingCalendar) -> None:
        self._path = Path(state_dir) / "analysis_schedules.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._calendar = calendar
        self._state: dict[str, AnalysisSchedule] = {}
        self._load()

    # ------------------------------------------------------------------
    def due_slots(self, now: datetime | None = None,
                  market_date_fresh: dict[str, str] | None = None
                  ) -> list[dict[str, Any]]:
        """Slots that should run. ``market_date_fresh`` maps market→latest
        data date; slots are skipped when market data is stale/missing."""
        now = now or datetime.now(timezone.utc)
        market_date_fresh = market_date_fresh or {}
        due: list[dict[str, Any]] = []
        for market, slot in _SLOTS:
            sched = self._state.get(f"{market}:{slot}") or AnalysisSchedule(
                schedule_id=f"{market}:{slot}", market=market, slot=slot)
            if not sched.enabled:
                continue
            fresh_date = market_date_fresh.get(market, "")
            if market in ("tw", "us") and not fresh_date:
                continue  # 行情未更新 → 不假裝完成分析
            if fresh_date and sched.last_market_date == fresh_date:
                continue  # already ran for this data vintage
            if not self._slot_due(market, slot, sched, now):
                continue
            due.append(sched.to_dict())
        return due

    def mark_ran(self, schedule_id: str, market_date: str,
                 now: datetime | None = None) -> dict[str, Any]:
        sched = self._state.get(schedule_id)
        if sched is None:
            market, _, slot = schedule_id.partition(":")
            sched = AnalysisSchedule(schedule_id=schedule_id,
                                     market=market, slot=slot)
            self._state[schedule_id] = sched
        sched.last_run_at = now or datetime.now(timezone.utc)
        sched.last_market_date = market_date
        sched.run_count += 1
        self._persist()
        return {"ok": True, "schedule": sched.to_dict()}

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        sched = self._state.get(schedule_id)
        if sched is None:
            market, _, slot = schedule_id.partition(":")
            sched = AnalysisSchedule(schedule_id=schedule_id,
                                     market=market, slot=slot)
            self._state[schedule_id] = sched
        sched.enabled = bool(enabled)
        self._persist()
        return {"ok": True, "schedule": sched.to_dict()}

    def list(self) -> list[dict[str, Any]]:
        known = {f"{m}:{s}" for m, s in _SLOTS}
        out = [self._state[k].to_dict() for k in sorted(self._state)]
        covered = {s["schedule_id"] for s in out}
        for sid in sorted(known - covered):
            market, _, slot = sid.partition(":")
            out.append(AnalysisSchedule(
                schedule_id=sid, market=market, slot=slot).to_dict())
        return out

    # ------------------------------------------------------------------
    def _slot_due(self, market: str, slot: str,
                  sched: AnalysisSchedule, now: datetime) -> bool:
        if market in ("tw", "us"):
            bounds = self._calendar.session_bounds_utc(market, now.date())
            if bounds is None:
                return False
            open_utc, close_utc = bounds
            if not self._calendar.is_trading_day(market, now.date()):
                return False
            if slot == "pre_open":
                return now < open_utc
            if slot == "intraday":
                return open_utc <= now < close_utc
            if slot == "post_close":
                return now >= close_utc
            return False
        if slot == "nav_update":
            return True
        if slot == "daily":
            return sched.last_run_at is None or (
                now.date() > sched.last_run_at.date())
        if slot == "weekly":
            return sched.last_run_at is None or (
                (now - sched.last_run_at).days >= 7)
        if slot == "monthly":
            return sched.last_run_at is None or (
                (now - sched.last_run_at).days >= 28)
        return False

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text("utf-8"))
        except ValueError:
            return
        for row in rows:
            s = AnalysisSchedule(
                schedule_id=row["schedule_id"], market=row["market"],
                slot=row["slot"], enabled=row.get("enabled", True),
                last_market_date=row.get("last_market_date", ""),
                run_count=int(row.get("run_count") or 0))
            if row.get("last_run_at"):
                try:
                    s.last_run_at = datetime.fromisoformat(row["last_run_at"])
                except ValueError:
                    pass
            self._state[s.schedule_id] = s

    def _persist(self) -> None:
        rows = [s.to_dict() for s in self._state.values()]
        self._path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
