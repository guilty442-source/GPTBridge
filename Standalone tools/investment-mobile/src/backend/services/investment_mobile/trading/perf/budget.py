"""InvestmentResourceBudget — formal resource limits (§20).

Limits come from governed settings only; AI/model actors can NEVER
raise a budget. When a budget is exhausted the caller degrades
deterministically (research deferred, reports delayed) — risk and state
recovery capacity are never denied.
"""
from __future__ import annotations

from typing import Any

DEFAULT_LIMITS: dict[str, int] = {
    "cpu_workers": 4,              # strategy/analysis parallelism
    "cache_entries": 2000,         # InvestmentCachePolicy capacity
    "inference_concurrency": 2,    # model calls in flight
    "db_connections": 4,           # bounded pool
    "background_jobs": 32,         # InvestmentJobManager queue cap
    "history_rows": 50_000,        # max rows a single query may load
    "event_queue": 5000,           # pending trading events
    "inference_queue": 64,         # pending AI work items
}

# Work kinds whose denial would compromise safety — always admitted.
_ALWAYS_ADMIT = frozenset({"risk", "recovery", "audit", "halt"})


class InvestmentResourceBudget:
    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = dict(DEFAULT_LIMITS)
        if limits:
            self._limits.update({k: int(v) for k, v in limits.items()})
        self._in_use: dict[str, int] = {}
        self._denied: dict[str, int] = {}

    def set_limit(self, key: str, value: int, *, actor: str) -> dict[str, Any]:
        """Limits are governance-owned — AI actors may only tighten."""
        if key not in self._limits:
            return {"ok": False, "error_code": "BUDGET_KEY_UNKNOWN"}
        if actor in ("ai", "model", "strategy") and int(value) > self._limits[key]:
            return {"ok": False, "error_code": "AI_CANNOT_RAISE_BUDGET"}
        self._limits[key] = int(value)
        return {"ok": True, "limits": dict(self._limits)}

    def limit(self, key: str) -> int:
        return self._limits.get(key, 0)

    def acquire(self, key: str, *, work_kind: str = "") -> dict[str, Any]:
        if key not in self._limits:
            return {"ok": False, "error_code": "BUDGET_KEY_UNKNOWN"}
        if work_kind in _ALWAYS_ADMIT:
            return {"ok": True, "admitted": "safety"}
        used = self._in_use.get(key, 0)
        if used >= self._limits[key]:
            self._denied[key] = self._denied.get(key, 0) + 1
            return {"ok": False, "error_code": "BUDGET_EXHAUSTED",
                    "key": key, "limit": self._limits[key]}
        self._in_use[key] = used + 1
        return {"ok": True, "in_use": used + 1}

    def release(self, key: str) -> None:
        self._in_use[key] = max(0, self._in_use.get(key, 0) - 1)

    def queued_headroom(self, key: str) -> int:
        return max(0, self._limits.get(key, 0) - self._in_use.get(key, 0))

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "limits": dict(self._limits),
            "in_use": dict(self._in_use),
            "denied": dict(self._denied),
            "always_admitted": sorted(_ALWAYS_ADMIT),
            "note": "AI/策略不得自行提高上限",
        }
