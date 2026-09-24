"""StrategyScheduler + TradingSessionController.

Jobs are event-driven or calendar-driven — never busy-polling. Kinds:
    event | fixed_time | interval | market_open | market_close |
    quote_update | nav_update

Each job supports pause / resume / cancel / timeout / bounded retry.
``due()`` only returns jobs whose trigger fired and whose run-key
hasn't already executed — idempotent per data vintage, same contract as
the monitoring scheduler.

TradingSessionController gates strategy order flow by market session
(zoneinfo via TradingCalendar — Taiwan time never hardcoded), allowed
sessions/instrument types/directions and a max-order frequency cap.
Strategies cannot alter market hours.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StrategyScheduler:
    JOB_KINDS = frozenset({
        "event", "fixed_time", "interval", "market_open",
        "market_close", "quote_update", "nav_update"})

    def __init__(self, state_dir: Path, calendar: Any,
                 *, default_timeout_s: float = 30.0,
                 max_retries: int = 2) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "jobs.json"
        self._jobs: dict[str, dict[str, Any]] = {}
        self._done: set[str] = set()
        self._calendar = calendar
        self._timeout = default_timeout_s
        self._max_retries = max_retries
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._jobs = data.get("jobs", {})
            self._done = set(data.get("done", []))
        except Exception:
            self._jobs, self._done = {}, set()

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            {"jobs": self._jobs, "done": sorted(self._done)[-5000:]},
            ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------
    def schedule(self, *, run_id: str, kind: str, market: str = "",
                 interval_s: float = 0, at_time: str = "",
                 event_types: list[str] | None = None,
                 timeout_s: float | None = None) -> dict[str, Any]:
        if kind not in self.JOB_KINDS:
            return {"ok": False, "error_code": "JOB_KIND_UNKNOWN"}
        job = {
            "job_id": f"job-{uuid.uuid4().hex[:10]}",
            "run_id": str(run_id), "kind": kind,
            "market": str(market),
            "interval_s": float(interval_s),
            "at_time": str(at_time),          # HH:MM local market time
            "event_types": [str(e) for e in (event_types or [])],
            "enabled": True, "status": "scheduled",
            "timeout_s": float(timeout_s or self._timeout),
            "retry_limit": self._max_retries,
            "retries": 0, "last_run": 0.0,
            "created_at": time.time(),
        }
        self._jobs[job["job_id"]] = job
        self._persist()
        return {"ok": True, "job": dict(job)}

    def control(self, job_id: str, action: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error_code": "JOB_NOT_FOUND"}
        if action not in ("pause", "resume", "cancel"):
            return {"ok": False, "error_code": "ACTION_UNKNOWN"}
        job["enabled"] = action == "resume"
        job["status"] = {"pause": "paused", "resume": "scheduled",
                         "cancel": "cancelled"}[action]
        self._persist()
        return {"ok": True, "job": dict(job)}

    # ------------------------------------------------------------------
    def _triggered(self, job: dict[str, Any], now: float) -> str:
        """Return a run-key when due, '' otherwise."""
        kind = job["kind"]
        if kind == "interval":
            if now - job["last_run"] >= job["interval_s"]:
                return f"{job['job_id']}|{int(now // max(job['interval_s'], 1))}"
            return ""
        if kind in ("market_open", "market_close"):
            market = job["market"]
            today = datetime.now(timezone.utc).date()
            if not self._calendar.is_trading_day(market, today):
                return ""
            bounds = self._calendar.session_bounds_utc(market, today)
            if bounds is None:
                return ""
            open_utc, close_utc = bounds
            edge = open_utc if kind == "market_open" else close_utc
            if now >= edge.timestamp() and job["last_run"] < edge.timestamp():
                return f"{job['job_id']}|{today.isoformat()}|{kind}"
            return ""
        if kind == "fixed_time":
            # at_time = HH:MM in market-local time
            try:
                hh, mm = (int(x) for x in job["at_time"].split(":"))
            except Exception:
                return ""
            market = job["market"] or "tw"
            local = self._calendar.local_now(market)
            target = local.replace(hour=hh, minute=mm,
                                   second=0, microsecond=0)
            if (local >= target
                    and job["last_run"] < target.timestamp()):
                return f"{job['job_id']}|{local.date().isoformat()}"
            return ""
        return ""     # event kinds are triggered via on_event()

    def due(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        out = []
        for job in self._jobs.values():
            if not job["enabled"] or job["status"] == "cancelled":
                continue
            key = self._triggered(job, now)
            if key and key not in self._done:
                out.append({**job, "run_key": key})
        return {"ok": True, "due": out}

    def on_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Event-driven triggers — fired once per event fingerprint."""
        et = event.get("event_type", "")
        market = event.get("market", "")
        out = []
        for job in self._jobs.values():
            if not job["enabled"] or job["status"] == "cancelled":
                continue
            if job["kind"] != "event" or et not in job["event_types"]:
                continue
            if job["market"] and market and job["market"] != market:
                continue
            key = f"{job['job_id']}|{event['fingerprint']}"
            if key in self._done:
                continue
            out.append({**job, "run_key": key, "event": event})
        return {"ok": True, "due": out}

    def mark_ran(self, run_key: str, *, ok: bool = True,
                 error: str = "") -> dict[str, Any]:
        job_id = run_key.split("|")[0]
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error_code": "JOB_NOT_FOUND"}
        if ok:
            self._done.add(run_key)
            job["last_run"] = time.time()
            job["retries"] = 0
            job["status"] = "scheduled"
        else:
            job["retries"] += 1
            job["status"] = ("failed" if job["retries"]
                             > job["retry_limit"] else "retrying")
            job["last_error"] = error
            if job["status"] == "failed":
                self._done.add(run_key)   # bounded — stop retrying
        self._persist()
        return {"ok": True, "job": dict(job)}

    def status(self) -> dict[str, Any]:
        return {"ok": True,
                "jobs": [dict(j) for j in self._jobs.values()],
                "completed": len(self._done)}


class TradingSessionController:
    """Per-market session + strategy order-flow gating."""

    def __init__(self, calendar: Any) -> None:
        self._calendar = calendar

    def session(self, market: str,
                at: datetime | None = None) -> dict[str, Any]:
        at = at or datetime.now(timezone.utc)
        local = self._calendar.local_now(market)
        return {"market": market,
                "session": self._calendar.session_for(market, at),
                "open": self._calendar.is_open(market, at),
                "trading_day": self._calendar.is_trading_day(
                    market, local.date())}

    def allow_order(self, run: dict[str, Any], *, instrument_id: str,
                    side: str, instrument_type: str = "stock",
                    recent_orders: int = 0,
                    at: datetime | None = None) -> dict[str, Any]:
        """Check a strategy's session policy. The policy is strategy
        config — it can only ever narrow market sessions, never widen
        them."""
        market = run.get("market") or ""
        pol = dict(run.get("session_policy") or {})
        sess = self._calendar.session_for(
            market, at or datetime.now(timezone.utc))
        allowed_sessions = pol.get("sessions") or ["regular"]
        if sess not in allowed_sessions:
            return {"ok": False, "error_code": "SESSION_BLOCKED",
                    "session": sess,
                    "allowed_sessions": allowed_sessions,
                    "note": "盤前/盤後/休市未在策略允許時段"}
        types = pol.get("instrument_types")
        if types and instrument_type not in types:
            return {"ok": False, "error_code": "INSTRUMENT_TYPE_BLOCKED"}
        dirs = pol.get("directions")
        if dirs and side not in dirs:
            return {"ok": False, "error_code": "DIRECTION_BLOCKED"}
        max_per_day = int(pol.get("max_orders_per_day") or 0)
        if max_per_day and recent_orders >= max_per_day:
            return {"ok": False, "error_code": "FREQUENCY_CAP",
                    "cap": max_per_day}
        return {"ok": True, "session": sess}
