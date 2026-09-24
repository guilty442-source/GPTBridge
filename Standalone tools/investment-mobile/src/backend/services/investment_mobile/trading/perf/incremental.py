"""IncrementalIndicatorState — O(1)-per-bar indicators (§4).

Keeps rolling state so a new completed bar updates only the affected
indicator state — never rescans history. Supports checkpoint/restore so
a restart resumes from the last state instead of recomputing. A bar
correction rewinds to the affected window only (bounded by the longest
window, not the full series).
"""
from __future__ import annotations

from collections import deque
from typing import Any

WINDOWS = (5, 20, 50, 200)


class IncrementalIndicatorState:
    def __init__(self, *, periods: tuple[int, ...] = WINDOWS,
                 rsi_period: int = 14) -> None:
        self._periods = tuple(periods)
        self._rsi_p = rsi_period
        self._closes: deque[float] = deque(maxlen=max(periods) + 1)
        self._sma_sums = {p: 0.0 for p in periods}
        self._ema: dict[int, float | None] = {p: None for p in periods}
        self._ema12: float | None = None
        self._ema26: float | None = None
        self._macd_sig: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._peak: float | None = None
        self._max_dd = 0.0
        self.count = 0

    # --------------------------------------------------------------
    def append_bar(self, close: float) -> dict[str, Any]:
        """O(1) update — returns the refreshed indicator snapshot."""
        c = float(close)
        q = self._closes
        if len(q) == q.maxlen:
            evicted = q[0]
            for p in self._periods:
                if self.count >= p:
                    self._sma_sums[p] -= evicted if len(q) > p else 0.0
        # recompute rolling sums bounded by window (deque maxlen keeps
        # memory fixed; sum over at most `period` tail elements)
        q.append(c)
        for p in self._periods:
            self._sma_sums[p] = sum(list(q)[-p:]) if len(q) >= p else 0.0
        for p in self._periods:
            k = 2.0 / (p + 1)
            prev = self._ema[p]
            self._ema[p] = c if prev is None else prev + k * (c - prev)
        k12, k26 = 2.0 / 13, 2.0 / 27
        self._ema12 = c if self._ema12 is None else \
            self._ema12 + k12 * (c - self._ema12)
        self._ema26 = c if self._ema26 is None else \
            self._ema26 + k26 * (c - self._ema26)
        macd_line = (self._ema12 or 0.0) - (self._ema26 or 0.0)
        k9 = 2.0 / 10
        self._macd_sig = macd_line if self._macd_sig is None else \
            self._macd_sig + k9 * (macd_line - self._macd_sig)
        if self.count > 0 and len(q) >= 2:
            d = c - q[-2]
            g, l = max(d, 0.0), max(-d, 0.0)
            if self._avg_gain is None:
                if self.count >= self._rsi_p:
                    ds = [q[i + 1] - q[i] for i in range(len(q) - 1)]
                    ds = ds[-self._rsi_p:]
                    self._avg_gain = sum(max(x, 0) for x in ds) / self._rsi_p
                    self._avg_loss = sum(max(-x, 0) for x in ds) / self._rsi_p
            else:
                p = self._rsi_p
                self._avg_gain = (self._avg_gain * (p - 1) + g) / p
                self._avg_loss = (self._avg_loss * (p - 1) + l) / p
        self._peak = c if self._peak is None else max(self._peak, c)
        if self._peak:
            self._max_dd = min(self._max_dd, c / self._peak - 1.0)
        self.count += 1
        return self.snapshot()

    def correct_bar(self, index_from_end: int, close: float) -> dict[str, Any]:
        """Data correction — recompute only the affected tail window
        (bounded by the longest period), not the full history."""
        q = self._closes
        idx = len(q) + index_from_end  # negative index_from_end
        if idx < 0 or idx >= len(q):
            return {"ok": False, "error_code": "CORRECTION_OUT_OF_RANGE"}
        q_list = list(q)
        q_list[idx] = float(close)
        affected = q_list[max(0, idx - max(self._periods)):]
        # rebuild rolling state over the bounded affected window
        q.clear()
        self._sma_sums = {p: 0.0 for p in self._periods}
        for p in self._periods:
            self._ema[p] = None
        self._ema12 = self._ema26 = self._macd_sig = None
        self._avg_gain = self._avg_loss = None
        keep_peak, self._peak, self._max_dd = self._peak, None, 0.0
        keep_count = self.count - len(affected)
        self.count = 0
        for v in affected:
            self.append_bar(v)
        self.count += keep_count
        self._peak = self._peak if self._peak is not None else keep_peak
        return {"ok": True, "recomputed_bars": len(affected)}

    # --------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        q = self._closes
        out: dict[str, Any] = {"count": self.count,
                               "last": q[-1] if q else None}
        for p in self._periods:
            out[f"ma{p}"] = (
                round(self._sma_sums[p] / p, 6)
                if len(q) >= p else None)
        out["rsi14"] = (
            round(100.0 - 100.0 / (1.0 + self._avg_gain / self._avg_loss), 4)
            if self._avg_loss else
            (100.0 if self._avg_gain else None))
        macd = ((self._ema12 or 0.0) - (self._ema26 or 0.0)) \
            if self._ema12 is not None and self._ema26 is not None else None
        out["macd"] = round(macd, 6) if macd is not None else None
        out["macd_signal"] = round(self._macd_sig, 6) \
            if self._macd_sig is not None else None
        out["max_drawdown"] = round(self._max_dd, 6)
        return out

    # --------------------------------------------------------------
    def checkpoint(self) -> dict[str, Any]:
        return {
            "closes": list(self._closes),
            "ema": {str(k): v for k, v in self._ema.items()},
            "ema12": self._ema12, "ema26": self._ema26,
            "macd_sig": self._macd_sig,
            "avg_gain": self._avg_gain, "avg_loss": self._avg_loss,
            "peak": self._peak, "max_dd": self._max_dd,
            "count": self.count,
        }

    def restore(self, ckpt: dict[str, Any]) -> None:
        q = self._closes
        q.clear()
        q.extend(float(v) for v in ckpt.get("closes", []))
        for p in self._periods:
            self._sma_sums[p] = sum(list(q)[-p:]) if len(q) >= p else 0.0
            self._ema[p] = ckpt.get("ema", {}).get(str(p))
        self._ema12 = ckpt.get("ema12")
        self._ema26 = ckpt.get("ema26")
        self._macd_sig = ckpt.get("macd_sig")
        self._avg_gain = ckpt.get("avg_gain")
        self._avg_loss = ckpt.get("avg_loss")
        self._peak = ckpt.get("peak")
        self._max_dd = float(ckpt.get("max_dd") or 0.0)
        self.count = int(ckpt.get("count") or len(q))
