"""IPC command latency ledger (§10.11).

Bounded rolling window of command round-trip times recorded by the
command dispatch path. ``snapshot()`` projects p50/p95/p99 for the
perf-baseline injector — measured data only, never fabricated.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

_MAX_SAMPLES = 1024
_lock = threading.Lock()
_samples: deque[tuple[str, float]] = deque(maxlen=_MAX_SAMPLES)


def record(command: str, elapsed_ms: float) -> None:
    with _lock:
        _samples.append((str(command), float(elapsed_ms)))


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(len(values) * q)))
    return round(values[idx], 1)


def snapshot() -> dict[str, Any]:
    with _lock:
        rows = list(_samples)
    all_ms = [ms for _, ms in rows]
    per_command: dict[str, list[float]] = {}
    for command, ms in rows:
        per_command.setdefault(command, []).append(ms)
    return {
        "samples": len(rows),
        "p50_ms": _percentile(all_ms, 0.50),
        "p95_ms": _percentile(all_ms, 0.95),
        "p99_ms": _percentile(all_ms, 0.99),
        "per_command": {
            cmd: {
                "samples": len(vals),
                "p95_ms": _percentile(vals, 0.95),
            }
            for cmd, vals in sorted(per_command.items())
        },
    }


__all__ = ["record", "snapshot"]
