"""InvestmentInferenceScheduler — model-work scheduling (§7/§8/§9).

Wraps the existing AIAnalysisWorkloadManager (priority queue, cache,
degrade-safe). Adds: bounded pending queue (model-busy never
accumulates unbounded work), work expiry (stale analysis cancelled),
model health state, and GPU-budget consultation — investment-mobile
does NOT own GPU allocation; it schedules through the shared 星澄
channel and degrades to deterministic-only when the model/GPU is
unavailable.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

TASK_KINDS = ("MARKET_SUMMARY", "POSITION_ANALYSIS", "FUND_RESEARCH",
              "STRATEGY_RESEARCH", "INVESTMENT_REPORT")


class InvestmentInferenceScheduler:
    def __init__(self, workload: Any = None, budget: Any = None, *,
                 queue_key: str = "inference_queue",
                 concurrency_key: str = "inference_concurrency") -> None:
        self._workload = workload          # AIAnalysisWorkloadManager
        self._budget = budget              # InvestmentResourceBudget
        self._qkey = queue_key
        self._ckey = concurrency_key
        self._pending: deque[dict[str, Any]] = deque()
        self._in_flight = 0
        self._stats = {"submitted": 0, "expired": 0, "rejected": 0,
                       "completed": 0, "degraded": 0}

    # --------------------------------------------------------------
    def submit(self, kind: str, payload: dict[str, Any], *,
               priority: str = "RESEARCH",
               ttl_s: float = 300.0) -> dict[str, Any]:
        if kind not in TASK_KINDS:
            return {"ok": False, "error_code": "TASK_KIND_UNKNOWN"}
        cap = (self._budget.limit(self._qkey)
               if self._budget else 64)
        if len(self._pending) >= max(1, cap):
            self._stats["rejected"] += 1
            return {"ok": False, "error_code": "INFERENCE_QUEUE_FULL",
                    "note": "model busy — work not queued"}
        self._pending.append({
            "kind": kind, "payload": dict(payload),
            "priority": priority, "submitted_at": time.time(),
            "expires_at": time.time() + float(ttl_s),
        })
        self._stats["submitted"] += 1
        return {"ok": True, "queued": len(self._pending)}

    def expire_stale(self) -> int:
        now = time.time()
        kept = deque(w for w in self._pending if w["expires_at"] > now)
        expired = len(self._pending) - len(kept)
        self._pending = kept
        self._stats["expired"] += expired
        return expired

    # --------------------------------------------------------------
    async def run_next(self) -> dict[str, Any]:
        self.expire_stale()
        if not self._pending:
            return {"ok": False, "error_code": "NO_PENDING_WORK"}
        if self._budget:
            slot = self._budget.acquire(self._ckey, work_kind="inference")
            if not slot.get("ok"):
                return {"ok": False, "error_code": "INFERENCE_BUSY"}
        work = self._pending.popleft()
        self._in_flight += 1
        try:
            if self._workload is None:
                self._stats["degraded"] += 1
                return {"ok": True, "degraded": True,
                        "note": "model unavailable — deterministic only"}
            res = await self._workload.analyze(
                work["kind"], work["payload"],
                priority=work["priority"])
            if res.get("degraded"):
                self._stats["degraded"] += 1
            else:
                self._stats["completed"] += 1
            return res
        finally:
            self._in_flight -= 1
            if self._budget:
                self._budget.release(self._ckey)

    # --------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        available = self._workload is not None
        return {
            "ok": True,
            "model_available": available,
            "pending": len(self._pending),
            "in_flight": self._in_flight,
            **self._stats,
            "note": "GPU/model unavailable → research deferred, "
                    "deterministic market/risk continues",
        }
