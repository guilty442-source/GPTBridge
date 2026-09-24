"""StrategyExecutionPool — bounded strategy work execution (§6).

One pool serves TW/US/ETF/fund strategies; no per-instrument threads and
no per-strategy processes. Work items carry priority/timeout/
cancellation/resource_budget/execution_id. Identical work (same
strategy + data revision + params) is deduplicated.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
from typing import Any, Awaitable, Callable

WorkFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class StrategyExecutionPool:
    def __init__(self, budget: Any = None, *,
                 max_concurrent: int = 4) -> None:
        self._budget = budget
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._ids = itertools.count(1)
        self._inflight_keys: dict[str, str] = {}   # dedup key -> exec_id
        self._cancelled: set[str] = set()
        self._stats = {"submitted": 0, "deduplicated": 0,
                       "completed": 0, "failed": 0, "timed_out": 0,
                       "cancelled": 0, "rejected": 0}

    def _key(self, work: dict[str, Any]) -> str:
        raw = json.dumps(
            {"s": work.get("strategy_id"), "v": work.get("data_revision"),
             "p": work.get("params")}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def run(self, work: dict[str, Any], fn: WorkFn, *,
                  priority: int = 5,
                  timeout_s: float = 30.0,
                  resource_budget: str = "cpu_workers") -> dict[str, Any]:
        eid = f"exec-{next(self._ids)}"
        key = self._key(work)
        if key in self._inflight_keys:
            self._stats["deduplicated"] += 1
            return {"ok": True, "deduplicated": True,
                    "execution_id": self._inflight_keys[key]}
        slot = (self._budget.acquire(resource_budget, work_kind="strategy")
                if self._budget else {"ok": True})
        if not slot.get("ok"):
            self._stats["rejected"] += 1
            return slot
        self._stats["submitted"] += 1
        self._inflight_keys[key] = eid
        try:
            async with self._sem:
                if eid in self._cancelled:
                    self._stats["cancelled"] += 1
                    return {"ok": False, "error_code": "CANCELLED",
                            "execution_id": eid}
                try:
                    res = await asyncio.wait_for(
                        fn({**work, "execution_id": eid,
                            "priority": priority}),
                        timeout=timeout_s)
                    self._stats["completed"] += 1
                    return {"ok": True, "execution_id": eid,
                            "result": res}
                except asyncio.TimeoutError:
                    self._stats["timed_out"] += 1
                    return {"ok": False, "error_code": "TIMEOUT",
                            "execution_id": eid}
                except asyncio.CancelledError:
                    self._stats["cancelled"] += 1
                    return {"ok": False, "error_code": "CANCELLED",
                            "execution_id": eid}
                except Exception as exc:
                    self._stats["failed"] += 1
                    return {"ok": False,
                            "error_code": type(exc).__name__,
                            "execution_id": eid}
        finally:
            self._inflight_keys.pop(key, None)
            self._cancelled.discard(eid)
            if self._budget:
                self._budget.release(resource_budget)

    def cancel(self, execution_id: str) -> dict[str, Any]:
        self._cancelled.add(str(execution_id))
        return {"ok": True, "execution_id": execution_id}

    def stats(self) -> dict[str, Any]:
        return {"ok": True, **self._stats,
                "in_flight": len(self._inflight_keys),
                "note": "bounded concurrency; identical "
                        "strategy+revision+params deduplicated"}
