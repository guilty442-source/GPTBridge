"""InvestmentRuntimeMetrics — bounded in-process counters/latency rings.

All series are ring-bounded; no sensitive account/trade payload is ever
recorded — only command names, latencies and counts (§21).
"""
from __future__ import annotations

import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

_MAX_SAMPLES = 256


class InvestmentRuntimeMetrics:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lat: dict[str, deque[float]] = {}
        self._started = time.monotonic()

    def incr(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    def observe(self, name: str, ms: float) -> None:
        self._lat.setdefault(name, deque(maxlen=_MAX_SAMPLES)).append(ms)

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        t = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - t) * 1000.0)

    def snapshot(self) -> dict[str, Any]:
        lat = {}
        for name, buf in self._lat.items():
            if not buf:
                continue
            s = sorted(buf)
            lat[name] = {
                "n": len(s),
                "avg_ms": round(sum(s) / len(s), 3),
                "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 3),
                "max_ms": round(s[-1], 3),
            }
        return {
            "ok": True,
            "uptime_s": round(time.monotonic() - self._started, 1),
            "counters": dict(self._counters),
            "latency": lat,
            "note": "bounded rings; no account/trade payload recorded",
        }
