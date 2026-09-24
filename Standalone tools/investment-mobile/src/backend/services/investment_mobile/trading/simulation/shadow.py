"""ShadowTradingService + SignalOutcomeTracker.

SHADOW mode: AI keeps analyzing real market data and emits immutable
signal records — originals are never rewritten when the market moves.
Outcome tracking measures signal-price path (MFE/MAE/return at 1/5/20/
60-bar horizons) — explicitly NOT traded performance.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..market.history import CandleStore
from ..fund.engine import MutualFundEngine
from .contracts import ShadowSignal

_HORIZONS = (1, 5, 20, 60)


class ShadowTradingService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "shadow_signals.jsonl"
        self._fp = None

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def record(self, signal: ShadowSignal) -> dict[str, Any]:
        if signal.side not in ("BUY", "SELL", "HOLD", "ADD", "REDUCE",
                               "EXIT", "SUBSCRIBE", "REDEEM", "SWITCH"):
            return {"ok": False, "error_code": "SIDE_UNKNOWN"}
        row = signal.to_dict()
        if self._fp is None:
            self._fp = self._path.open("a", encoding="utf-8")
        self._fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fp.flush()
        return {"ok": True, "signal": row}

    def signals(self, instrument_id: str | None = None,
                limit: int = 200) -> list[dict[str, Any]]:
        rows = self._read()
        if instrument_id:
            rows = [r for r in rows
                    if r["instrument_id"] == instrument_id]
        return rows[-limit:]

    def _read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [json.loads(l) for l in
                self._path.read_text("utf-8").splitlines() if l.strip()]


class SignalOutcomeTracker:
    """Post-signal market-path measurement — not traded performance."""

    def __init__(self, candles: CandleStore,
                 fund_engine: MutualFundEngine) -> None:
        self._candles = candles
        self._fund = fund_engine

    def evaluate(self, signal: dict[str, Any],
                 timeframe: str = "1d") -> dict[str, Any]:
        iid = signal["instrument_id"]
        ref = Decimal(str(signal.get("reference_price") or 0))
        if signal.get("market") == "MUTUAL_FUND":
            parts = iid.split(":")
            fid = parts[1] if len(parts) > 1 else iid
            cls = parts[2] if len(parts) > 2 else "A"
            navs = self._fund.nav.history(fid, cls)
            series = [Decimal(str(n.nav)) for n in navs]
            basis = "nav"
        else:
            bars = self._candles.candles(iid, timeframe)
            series = [Decimal(str(b.close)) for b in bars]
            basis = "close"
        if ref <= 0 and series:
            ref = series[0]
        if ref <= 0 or not series:
            return {"ok": False, "error_code": "NO_SERIES"}

        sig_idx = 0
        if signal.get("created_at"):
            ts = float(signal["created_at"])
            for i, b in enumerate(
                    self._candles.candles(iid, timeframe)
                    if basis == "close" else []):
                if b.candle_end.timestamp() >= ts:
                    sig_idx = i
                    break

        direction = signal.get("side", "HOLD")
        sign = -1 if direction in ("SELL", "REDUCE", "EXIT", "REDEEM") \
            else 1
        horizons: dict[str, Any] = {}
        for h in _HORIZONS:
            end = min(sig_idx + h, len(series) - 1)
            path = series[sig_idx:end + 1]
            if not path:
                continue
            rets = [float((p / ref - 1) * sign) for p in path]
            horizons[str(h)] = {
                "bars": h, "end_value": str(series[end]),
                "return": f"{rets[-1]:.6f}",
                "mfe": f"{max(rets):.6f}", "mae": f"{min(rets):.6f}",
            }
        return {
            "ok": True, "signal_id": signal["signal_id"],
            "instrument_id": iid, "basis": basis,
            "reference_price": str(ref), "horizons": horizons,
            "note": "訊號追蹤非成交績效",
            "evaluated_at": time.time(),
        }
