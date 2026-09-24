"""AI strategy research: improvement proposals, experiment pipeline,
overfitting guard.

- AIStrategyImprovementService — 星澄 studies performance/backtest/
  SHADOW/PAPER evidence and drafts improvements. AI can never edit a
  RUNNING strategy: every proposal lands as a NEW draft version.
- StrategyExperimentManager — governed research pipeline:
  ACTIVE_STRATEGY → AI_RESEARCH → NEW_DRAFT → BACKTEST → VALIDATION
  → OUT_OF_SAMPLE → SHADOW → PAPER → ELIGIBLE_FOR_REVIEW.
  A better simulated result never replaces a live strategy by itself —
  replacement requires formal authorization.
- StrategyOverfittingGuard — records every evaluation's dataset key.
  Reusing the same out-of-sample dataset after parameter changes no
  longer counts as unseen OOS validation.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

AI_ACTORS = frozenset({"ai", "xingcheng", "model", "assistant"})

EXPERIMENT_STAGES = (
    "ACTIVE_STRATEGY", "AI_RESEARCH", "NEW_DRAFT", "BACKTEST",
    "VALIDATION", "OUT_OF_SAMPLE", "SHADOW", "PAPER",
    "ELIGIBLE_FOR_REVIEW")

_EVAL_KINDS = frozenset({"in_sample", "validation", "out_of_sample",
                         "shadow", "paper", "backtest"})


class StrategyOverfittingGuard:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "eval-ledger.jsonl"
        self._evals: list[dict[str, Any]] = []
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                self._evals.append(json.loads(line))
            except Exception:
                continue

    @staticmethod
    def dataset_key(dataset: dict[str, Any] | str) -> str:
        raw = dataset if isinstance(dataset, str) else json.dumps(
            dataset, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------
    def record(self, *, strategy_id: str, version: int,
               params: dict[str, Any], dataset: dict[str, Any] | str,
               kind: str, metrics: dict[str, Any]) -> dict[str, Any]:
        if kind not in _EVAL_KINDS:
            return {"ok": False, "error_code": "EVAL_KIND_UNKNOWN"}
        dkey = self.dataset_key(dataset)
        pkey = hashlib.sha256(json.dumps(
            params, sort_keys=True, ensure_ascii=False
        ).encode()).hexdigest()[:12]
        prior_oos = [e for e in self._evals
                     if e["strategy_id"] == strategy_id
                     and e["kind"] == "out_of_sample"
                     and e["dataset_key"] == dkey]
        contaminated = bool(prior_oos) and kind == "out_of_sample" and \
            prior_oos[0]["params_key"] != pkey
        row = {
            "eval_id": f"evl-{uuid.uuid4().hex[:10]}",
            "strategy_id": strategy_id, "version": int(version),
            "params_key": pkey, "dataset_key": dkey, "kind": kind,
            "metrics": dict(metrics), "at": time.time(),
            "oos_contaminated": contaminated,
        }
        self._evals.append(row)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        out = {"ok": True, "eval": dict(row)}
        if contaminated:
            out["warning"] = (
                "OOS_CONTAMINATED — 同一樣本外資料已在不同參數下使用過；"
                "本次不得宣稱為未使用過的樣本外驗證")
        return out

    def history(self, strategy_id: str | None = None
                ) -> list[dict[str, Any]]:
        return [dict(e) for e in self._evals
                if strategy_id is None
                or e["strategy_id"] == strategy_id]


class AIStrategyImprovementService:
    """Draft-only improvements — AI never touches a RUNNING strategy."""

    def __init__(self, state_dir: Path, registry: Any,
                 workload: Any | None = None) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "improvements.jsonl"
        self._registry = registry           # StrategyRegistry + versions
        self._workload = workload
        self._proposals: list[dict[str, Any]] = []
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                self._proposals.append(json.loads(line))
            except Exception:
                continue

    # ------------------------------------------------------------------
    async def research(self, run: dict[str, Any], *,
                       evidence: dict[str, Any]) -> dict[str, Any]:
        """Study evidence → proposal + NEW draft version (never edits
        the running one)."""
        det = {
            "strategy_id": run["strategy_id"],
            "current_version": run["strategy_version"],
            "evidence_keys": sorted(evidence),
        }
        model_text, model_id = "", "deterministic-engine"
        if self._workload is not None:
            r = await self._workload.analyze(
                "REPORT_GENERATION",
                {"task": "strategy_improvement", **det},
                priority="RESEARCH",
                data_vintage=str(evidence.get("data_vintage") or ""))
            model_text = str(r.get("model_text") or "")
            model_id = str(r.get("model_id") or model_id)
            det["degraded"] = bool(r.get("degraded"))
        proposal = {
            "proposal_id": f"imp-{uuid.uuid4().hex[:10]}",
            "strategy_id": run["strategy_id"],
            "base_version": run["strategy_version"],
            "candidate_parameters": dict(evidence.get(
                "candidate_parameters") or run.get("parameters") or {}),
            "risk_suggestions": list(evidence.get(
                "risk_suggestions") or []),
            "frequency_suggestions": list(evidence.get(
                "frequency_suggestions") or []),
            "scope_study": list(evidence.get("scope_study") or []),
            "evidence": det,
            "model_id": model_id, "model_text": model_text,
            "status": "draft", "at": time.time(),
            "note": "改良僅建立新版本草稿——RUNNING 策略不被修改；"
                    "替換需正式授權",
        }
        self._proposals.append(proposal)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(proposal, ensure_ascii=False) + "\n")
        return {"ok": True, "proposal": dict(proposal)}

    def list(self, strategy_id: str | None = None
             ) -> list[dict[str, Any]]:
        return [dict(p) for p in self._proposals
                if strategy_id is None
                or p["strategy_id"] == strategy_id]


class StrategyExperimentManager:
    """Governed stage pipeline — higher sim profit never auto-replaces."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "experiments.jsonl"
        self._experiments: dict[str, dict[str, Any]] = {}
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            self._experiments[e["experiment_id"]] = e

    def _persist(self, exp: dict[str, Any]) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(exp, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    def start(self, *, strategy_id: str, base_version: int,
              candidate_parameters: dict[str, Any],
              proposal_id: str = "") -> dict[str, Any]:
        exp = {
            "experiment_id": f"exp-{uuid.uuid4().hex[:10]}",
            "strategy_id": str(strategy_id),
            "base_version": int(base_version),
            "candidate_parameters": dict(candidate_parameters),
            "proposal_id": proposal_id,
            "stage": "AI_RESEARCH",
            "history": [{"stage": "AI_RESEARCH", "at": time.time()}],
            "created_at": time.time(), "status": "active",
        }
        self._experiments[exp["experiment_id"]] = exp
        self._persist(exp)
        return {"ok": True, "experiment": dict(exp)}

    def advance(self, experiment_id: str, target: str, *,
                actor: str = "system",
                evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        exp = self._experiments.get(experiment_id)
        if exp is None:
            return {"ok": False, "error_code": "EXPERIMENT_NOT_FOUND"}
        if target not in EXPERIMENT_STAGES:
            return {"ok": False, "error_code": "STAGE_UNKNOWN"}
        cur = EXPERIMENT_STAGES.index(exp["stage"]) \
            if exp["stage"] in EXPERIMENT_STAGES else -1
        want = EXPERIMENT_STAGES.index(target)
        if want != cur + 1:
            return {"ok": False, "error_code": "STAGE_SKIP_DENIED",
                    "from": exp["stage"], "to": target,
                    "note": "實驗階段必須依序推進——不得跳過驗證"}
        # replacing/promoting to review needs a human actor
        if target == "ELIGIBLE_FOR_REVIEW" and actor.lower() in AI_ACTORS:
            return {"ok": False, "error_code": "AI_TRANSITION_DENIED"}
        exp["stage"] = target
        exp["history"].append({"stage": target, "at": time.time(),
                               "actor": actor,
                               "evidence": dict(evidence or {})})
        self._persist(exp)
        return {"ok": True, "experiment": dict(exp)}

    def list(self, strategy_id: str | None = None
             ) -> list[dict[str, Any]]:
        return [dict(e) for e in self._experiments.values()
                if strategy_id is None
                or e["strategy_id"] == strategy_id]
