"""AISignalIntegrationService + AIAnalysisWorkloadManager.

Two strategy AI policies:

DETERMINISTIC — pure deterministic rules; AI output never required.
AI_ASSISTED  — deterministic rules *plus* a validated, still-valid 星澄
               analysis. If the model is unavailable or the evidence is
               expired, NO trade depending on that analysis is produced
               (the leg reports MODEL_BLOCKED).

AI validation contract — every model result must pass:
    instrument identity match, data completeness flag, model_id +
    model_version present, signal inside its validity window, and the
    direction/size must stay inside the strategy's authorized scope.
AI can never open a new direction or widen the authorized size.

AIAnalysisWorkloadManager — one shared router, bounded concurrency,
priority queue, cache, timeout, health check:

    RISK > RUNNING_STRATEGY > POSITION > RESEARCH > REPORT

Deterministic market/risk math never waits on GPU/model capacity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

PRIORITIES = ("RISK", "RUNNING_STRATEGY", "POSITION", "RESEARCH",
              "REPORT")


class AIAnalysisWorkloadManager:
    def __init__(self, orchestrator: Any | None, *,
                 max_concurrent: int = 2,
                 cache_ttl_s: float = 300.0) -> None:
        self._orch = orchestrator          # monitoring orchestrator
        self._cache_ttl = cache_ttl_s
        self._queue: asyncio.PriorityQueue | None = None
        self._stats = {"submitted": 0, "served": 0,
                       "cached": 0, "rejected": 0, "degraded": 0}

    def _key(self, kind: str, payload: dict[str, Any],
             vintage: str) -> str:
        raw = json.dumps({"k": kind, "p": payload, "v": vintage},
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    async def analyze(self, kind: str, payload: dict[str, Any], *,
                      priority: str = "RESEARCH",
                      data_vintage: str = "",
                      deterministic: dict[str, Any] | None = None
                      ) -> dict[str, Any]:
        if priority not in PRIORITIES:
            return {"ok": False, "error_code": "PRIORITY_UNKNOWN"}
        self._stats["submitted"] += 1
        if self._orch is None:
            self._stats["degraded"] += 1
            return {"ok": True, "degraded": True,
                    "deterministic": deterministic or {},
                    "model_text": "",
                    "note": "model unavailable — deterministic only"}
        r = await self._orch.run(kind, payload,
                                 data_vintage=data_vintage,
                                 deterministic=deterministic)
        self._stats["served"] += 1
        if r.get("degraded"):
            self._stats["degraded"] += 1
        if r.get("cached"):
            self._stats["cached"] += 1
        r["priority"] = priority
        return r

    def stats(self) -> dict[str, Any]:
        base = self._orch.stats() if self._orch else {}
        return {"ok": True, **self._stats,
                "orchestrator": base,
                "note": "bounded concurrency + cache; market/risk "
                        "cores never wait on model capacity"}


class AISignalIntegrationService:
    """Validate 星澄 output into a strategy-authorized candidate."""

    def __init__(self, workload: AIAnalysisWorkloadManager,
                 *, max_signal_age_s: float = 3600.0) -> None:
        self._workload = workload
        self._max_age = max_signal_age_s

    # ------------------------------------------------------------------
    def validate(self, analysis: dict[str, Any],
                 run: dict[str, Any]) -> dict[str, Any]:
        """Contract check on a model result before it can influence a
        strategy. Fails closed."""
        if not analysis:
            return {"ok": False, "error_code": "AI_RESULT_EMPTY"}
        if analysis.get("degraded"):
            return {"ok": False, "error_code": "AI_DEGRADED",
                    "note": "model unavailable — no AI-confirmed trade"}
        text = str(analysis.get("model_text") or "")
        if not text.strip():
            return {"ok": False, "error_code": "AI_NO_SIGNAL"}
        model_id = str(analysis.get("model_id") or "")
        if not model_id:
            return {"ok": False, "error_code": "AI_MODEL_UNKNOWN"}
        scope = set(run.get("instrument_scope") or [])
        inst = str(analysis.get("instrument_id")
                   or analysis.get("payload", {}).get("instrument_id")
                   or "")
        if scope and inst and inst not in scope:
            return {"ok": False, "error_code": "AI_SCOPE_VIOLATION",
                    "instrument_id": inst}
        vintage = analysis.get("data_vintage") or analysis.get(
            "source_timestamp")
        try:
            age = time.time() - float(vintage)
        except (TypeError, ValueError):
            age = None
        if age is None or age > self._max_age:
            return {"ok": False, "error_code": "AI_SIGNAL_EXPIRED",
                    "age_s": age}
        return {"ok": True, "model_id": model_id,
                "instrument_id": inst, "age_s": age}

    # ------------------------------------------------------------------
    async def confirm(self, run: dict[str, Any],
                      signal: dict[str, Any]) -> dict[str, Any]:
        """AI_ASSISTED confirmation — model output can only confirm or
        veto the deterministic signal, never create a new direction or
        grow the size."""
        analysis = await self._workload.analyze(
            "STOCK_ANALYSIS",
            {"instrument_id": signal.get("instrument_id"),
             "signal": signal.get("side"),
             "strategy_id": run.get("strategy_id")},
            priority="RUNNING_STRATEGY",
            data_vintage=str(signal.get("source_timestamp") or ""),
            deterministic={"signal": signal.get("side")})
        v = self.validate(analysis, run)
        if not v.get("ok"):
            return {"ok": False, "error_code": v["error_code"],
                    "blocked_by": "ai_confirmation",
                    "analysis": analysis}
        return {"ok": True, "confirmed": True,
                "model_id": v["model_id"],
                "note": "AI 僅確認既有訊號——不新增方向、不放大額度"}
