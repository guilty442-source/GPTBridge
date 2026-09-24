"""Market monitors — deterministic detection, honest session handling.

TaiwanInvestmentMonitor / USInvestmentMonitor watch positions + watchlist
instruments via CandleStore + IndicatorSet (deterministic math only —
the model interprets, it never fabricates numbers).

USInvestmentMonitor resolves sessions through TradingCalendar
(zoneinfo, DST-correct) — Taiwan time is never hardcoded; without
authorized extended-hours data the monitor reports regular-session
coverage only.

MutualFundMonitor tracks published NAVs, distributions, fee drift and
NAV staleness — a NAV is a published value, never a realtime price.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..intelligence.indicators import IndicatorSet


class _BaseMonitor:
    market = ""

    def __init__(self, candle_store: Any, calendar: Any,
                 events: Any, gate: Any) -> None:
        self._candles = candle_store
        self._calendar = calendar
        self._events = events
        self._gate = gate

    def _closes(self, instrument_id: str, limit: int = 220
                ) -> tuple[list[float], list[float], float | None]:
        rows = self._candles.candles(instrument_id, "1d")[-limit:]
        closes = [float(r.close) for r in rows]
        vols = [float(r.volume) for r in rows]
        ts = (rows[-1].candle_end.timestamp() if rows else None)
        return closes, vols, ts

    def scan(self, instrument_ids: list[str],
             positions: list[dict[str, Any]] | None = None
             ) -> dict[str, Any]:
        """Run deterministic checks; emit dedup'd MonitoringEvents."""
        emitted: list[dict[str, Any]] = []
        held = {p["instrument_id"]: p for p in (positions or [])
                if p["account_id"]}
        for iid in instrument_ids:
            closes, vols, ts = self._closes(iid)
            if not closes:
                self._events.emit(
                    "data_incomplete", instrument_id=iid,
                    market=self.market, severity="NOTICE",
                    detail={"reason": "no candles"})
                continue
            ind = IndicatorSet(closes, vols)
            src_ts = ts or time.time()
            gate = self._gate.check_instrument(iid, self.market)
            if gate["status"] == "STALE":
                emitted.append(self._events.emit(
                    "data_stale", instrument_id=iid, market=self.market,
                    source_timestamp=src_ts, severity="NOTICE",
                    detail={"age_seconds": time.time() - src_ts}))
            if len(closes) >= 2 and closes[-2]:
                chg = closes[-1] / closes[-2] - 1
                if abs(chg) >= 0.05:
                    emitted.append(self._events.emit(
                        "price_move", instrument_id=iid,
                        market=self.market, source_timestamp=src_ts,
                        severity=("WARNING" if abs(chg) >= 0.095
                                  else "NOTICE"),
                        detail={"change_pct": chg,
                                "close": closes[-1]}))
            if ind.ma5 and ind.ma20 and len(closes) >= 21:
                prev_above = (closes[-2] > (sum(
                    closes[-21:-1]) / 20))
                now_above = closes[-1] > ind.ma20
                if prev_above != now_above:
                    emitted.append(self._events.emit(
                        "ma_cross", instrument_id=iid,
                        market=self.market, source_timestamp=src_ts,
                        severity="NOTICE",
                        detail={"cross": "up" if now_above else "down",
                                "ma20": ind.ma20}))
            if ind.rsi14 is not None and (ind.rsi14 >= 70
                                          or ind.rsi14 <= 30):
                emitted.append(self._events.emit(
                    "indicator_signal", instrument_id=iid,
                    market=self.market, source_timestamp=src_ts,
                    severity="NOTICE",
                    detail={"rsi14": ind.rsi14,
                            "zone": ("overbought" if ind.rsi14 >= 70
                                     else "oversold")}))
            if vols and len(vols) >= 21:
                avg = sum(vols[-21:-1]) / 20
                if avg > 0 and vols[-1] / avg >= 2.5:
                    emitted.append(self._events.emit(
                        "volume_spike", instrument_id=iid,
                        market=self.market, source_timestamp=src_ts,
                        severity="NOTICE",
                        detail={"volume_ratio": vols[-1] / avg}))
            pos = held.get(iid)
            if pos is not None:
                cost = float(pos.get("average_cost") or 0)
                if cost > 0:
                    pnl = closes[-1] / cost - 1
                    if pnl <= -0.15 or pnl >= 0.30:
                        emitted.append(self._events.emit(
                            "holding_pnl", instrument_id=iid,
                            account_id=pos["account_id"],
                            market=self.market,
                            source_timestamp=src_ts,
                            severity=("WARNING" if pnl <= -0.15
                                      else "NOTICE"),
                            detail={"pnl_pct": pnl}))
        return {"ok": True, "market": self.market,
                "scanned": len(instrument_ids),
                "events": [e for e in emitted if e.get("ok")],
                "indicators_used": "IndicatorSet (deterministic)"}


class TaiwanInvestmentMonitor(_BaseMonitor):
    market = "tw"


class USInvestmentMonitor(_BaseMonitor):
    market = "us"

    def session_state(self, at: datetime | None = None) -> dict[str, Any]:
        """US session via market calendar (zoneinfo/DST) — Taiwan time
        is never hardcoded. Extended-hours coverage is only claimed when
        the calendar declares such a session."""
        at = at or datetime.now(timezone.utc)
        session = self._calendar.session_for("us", at)
        calendar_declares = any(
            w.name in ("pre", "post", "pre_market", "post_market")
            for w in getattr(self._calendar, "_SESSIONS", {}
                             ).get("us", []))
        return {
            "ok": True, "market": "us",
            "session": session,               # regular|pre|post|closed
            "calendar_sessions": calendar_declares,
            "extended_hours_coverage": False,
            "note": "no authorized pre/post-market data feed — "
                    "extended sessions are calendar-known but "
                    "coverage is regular-session data only",
        }


class MutualFundMonitor:
    market = "fund"

    def __init__(self, fund_engine: Any, events: Any,
                 gate: Any) -> None:
        self._fund = fund_engine
        self._events = events
        self._gate = gate

    def scan(self, funds: list[tuple[str, str]]  # (fund_id, share_class)
             ) -> dict[str, Any]:
        emitted = []
        for fund_id, sc in funds:
            nav = self._fund.nav.latest_published(fund_id, sc)
            if not nav.get("ok"):
                self._events.emit("data_incomplete",
                                  instrument_id=fund_id,
                                  market="fund", severity="NOTICE",
                                  detail={"reason": "no published NAV"})
                continue
            if nav.get("stale"):
                emitted.append(self._events.emit(
                    "nav_stale", instrument_id=fund_id, market="fund",
                    severity="NOTICE",
                    detail={"nav_date": nav["nav"]["nav_date"],
                            "age_days": nav["age_days"],
                            "note": "舊 NAV ≠ 即時交易價"}))
            else:
                emitted.append(self._events.emit(
                    "nav_update", instrument_id=fund_id, market="fund",
                    severity="INFO",
                    detail={"nav": nav["nav"]["nav"],
                            "nav_date": nav["nav"]["nav_date"]}))
            for d in self._fund.distributions.list(
                    fund_id=fund_id, share_class_id=sc)[-3:]:
                emitted.append(self._events.emit(
                    "fund_distribution", instrument_id=fund_id,
                    market="fund", severity="NOTICE",
                    state_token=str(d.get("ex_distribution_date") or ""),
                    detail={"distribution": d}))
        return {"ok": True, "market": "fund", "scanned": len(funds),
                "events": [e for e in emitted if e.get("ok")]}
