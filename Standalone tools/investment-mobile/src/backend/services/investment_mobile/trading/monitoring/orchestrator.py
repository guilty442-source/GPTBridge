"""InvestmentAnalysisOrchestrator — bounded, cached, deduped 星澄 tasks.

Task kinds: MARKET_SUMMARY | STOCK_ANALYSIS | ETF_ANALYSIS |
FUND_ANALYSIS | PORTFOLIO_ANALYSIS | RISK_INTERPRETATION |
RECOMMENDATION_EXPLANATION | REPORT_GENERATION.

Rules:
- one shared model router (no per-instrument LLM instances),
- identical task+data-vintage is served from cache (dedup),
- bounded concurrency + per-task inference budget,
- model down → deterministic results are still produced and events are
  preserved; analysis is marked degraded, never silently dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

TASK_KINDS = frozenset({
    "MARKET_SUMMARY", "STOCK_ANALYSIS", "ETF_ANALYSIS", "FUND_ANALYSIS",
    "PORTFOLIO_ANALYSIS", "RISK_INTERPRETATION",
    "RECOMMENDATION_EXPLANATION", "REPORT_GENERATION",
})


class InvestmentAnalysisOrchestrator:
    def __init__(self, router: Any | None = None, *,
                 max_concurrent: int = 2,
                 cache_ttl_s: float = 300.0,
                 budget_s: float = 30.0) -> None:
        self._router = router
        self._sem = asyncio.Semaphore(max_concurrent)
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = cache_ttl_s
        self._budget = budget_s
        self._stats = {"tasks": 0, "cached": 0, "degraded": 0,
                       "failed": 0}

    # ------------------------------------------------------------------
    def _key(self, kind: str, payload: dict[str, Any],
             vintage: str) -> str:
        raw = json.dumps({"k": kind, "p": payload, "v": vintage},
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    async def run(self, kind: str, payload: dict[str, Any], *,
                  data_vintage: str = "",
                  deterministic: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
        if kind not in TASK_KINDS:
            return {"ok": False, "error_code": "TASK_KIND_UNKNOWN"}
        key = self._key(kind, payload, data_vintage)
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit["at"] < self._cache_ttl:
            self._stats["cached"] += 1
            return {**hit["result"], "cached": True}
        self._stats["tasks"] += 1
        result: dict[str, Any]
        async with self._sem:
            if self._router is None or not self._router.available:
                # degraded: deterministic numbers still returned
                self._stats["degraded"] += 1
                result = {"ok": True, "kind": kind,
                          "degraded": True,
                          "deterministic": deterministic or {},
                          "model_text": "",
                          "note": "model unavailable — events and "
                                  "deterministic results preserved"}
            else:
                try:
                    prompt = json.dumps({"task": kind,
                                         "payload": payload},
                                        ensure_ascii=False)
                    r = await asyncio.wait_for(
                        self._router.infer(
                            "quick_market", prompt),
                        timeout=self._budget)
                    result = {"ok": True, "kind": kind,
                              "degraded": bool(
                                  getattr(r, "degraded", False)),
                              "deterministic": deterministic or {},
                              "model_text": getattr(r, "text", ""),
                              "model_id": getattr(
                                  getattr(r, "record", None),
                                  "model_id", "")}
                except Exception as exc:
                    self._stats["failed"] += 1
                    result = {"ok": False, "kind": kind,
                              "error_code": "INFERENCE_FAILED",
                              "error": f"{type(exc).__name__}: {exc}",
                              "deterministic": deterministic or {}}
        if result.get("ok"):
            self._cache[key] = {"at": now, "result": result}
        return result

    def stats(self) -> dict[str, Any]:
        return {"ok": True, **self._stats,
                "cache_entries": len(self._cache),
                "model_available": bool(
                    self._router and self._router.available)}
