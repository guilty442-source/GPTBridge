"""ModelRouter — task-kind → shared 星澄 model runtime lanes.

No per-task model processes and no in-tool LLM management. Inference
goes through the governed channel to local-model/xingcheng; when the
model service is unavailable every call degrades to a marked
rule-only answer — trading risk controls never depend on the model.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .contracts import AnalysisTaskKind, ModelAnalysisRecord

# Task lanes — shared runtime, not per-task models.
_LANES = {
    AnalysisTaskKind.QUICK_MARKET: "fast",
    AnalysisTaskKind.FUND: "fast",
    AnalysisTaskKind.PORTFOLIO_RISK: "fast",
    AnalysisTaskKind.DEEP_RESEARCH: "research",
    AnalysisTaskKind.EARNINGS: "research",
    AnalysisTaskKind.REPORT: "research",
}

_MODEL_IDS = {
    "fast": "xingcheng-native",
    "research": "xingcheng-native",   # same runtime; lane = prompt budget
    "fallback": "deterministic-rules",
}


@dataclass
class InferenceResult:
    ok: bool
    text: str = ""
    structured: dict[str, Any] | None = None
    degraded: bool = False
    record: ModelAnalysisRecord | None = None


class ModelRouter:
    """Route analysis tasks to the shared governed model channel."""

    def __init__(
        self,
        consult: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        model_ids: dict[str, str] | None = None,
        available_probe: Callable[[], bool] | None = None,
    ) -> None:
        # consult(prompt, source) -> governed ai response dict
        self._consult = consult
        self._probe = available_probe
        self._models = dict(_MODEL_IDS | (model_ids or {}))
        self._records: list[ModelAnalysisRecord] = []

    @property
    def available(self) -> bool:
        if self._consult is None:
            return False
        return bool(self._probe()) if self._probe else True

    # ------------------------------------------------------------------
    def lane_for(self, task_kind: str) -> str:
        return _LANES.get(task_kind, "fast")

    def model_for(self, task_kind: str) -> str:
        return self._models[self.lane_for(task_kind)]

    async def infer(
        self, task_kind: str, prompt: str, *,
        run_id: str = "", fallback_text: str = "",
    ) -> InferenceResult:
        """One inference. Never raises — degraded result on failure."""
        lane = self.lane_for(task_kind)
        model_id = self._models[lane]
        t0 = time.monotonic()
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        if not self.available:
            rec = ModelAnalysisRecord(
                run_id=run_id, task_kind=task_kind,
                model_id=self._models["fallback"],
                prompt_hash=prompt_hash, status="skipped",
            )
            self._records.append(rec)
            return InferenceResult(
                ok=True, text=fallback_text, degraded=True, record=rec)
        try:
            res = await self._consult(prompt, "investment-mobile")
        except Exception:
            res = {"ok": False}
        latency = (time.monotonic() - t0) * 1000.0
        ok = res.get("ok") is not False
        rec = ModelAnalysisRecord(
            run_id=run_id, task_kind=task_kind, model_id=model_id,
            model_version=str(res.get("model_version") or ""),
            prompt_hash=prompt_hash, latency_ms=latency,
            tokens_out=int(res.get("tokens_out") or 0),
            status="ok" if ok else "failed",
        )
        self._records.append(rec)
        if not ok:
            rec.status = "degraded"
            return InferenceResult(
                ok=True, text=fallback_text, degraded=True, record=rec)
        return InferenceResult(
            ok=True,
            text=str(res.get("response") or res.get("text") or ""),
            structured=res.get("structured")
            if isinstance(res.get("structured"), dict) else None,
            record=rec)

    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "available": self.available,
            "models": dict(self._models),
            "lanes": dict(_LANES),
            "inferences": len(self._records),
            "note": "模型不可用時僅降級分析，風控與行情不受影響",
        }

    def records(self, limit: int = 200) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._records[-limit:]]
