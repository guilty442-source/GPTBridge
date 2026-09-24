"""StrategyRuntimeState + MultiStrategyManager.

Formal state machine — transitions are governed, never free-text:

    CREATED → READY → RUNNING ⇄ PAUSED
                 RUNNING → RISK_HALTED | DATA_BLOCKED | MODEL_BLOCKED
                           | FAILED | STOPPED
                 {RISK_HALTED, DATA_BLOCKED, MODEL_BLOCKED, FAILED}
                     → RECOVERING → RUNNING | PAUSED | FAILED | STOPPED

Safety holds (RISK_HALTED) can only leave through RECOVERING, and the
recovery service decides — "the model said keep trading" never lifts a
halt. AI actors can never resume/unhalt a strategy.

MultiStrategyManager keeps each strategy's id, version, market, scope,
parameters, capital, risk budget, execution mode and status isolated —
strategies cannot touch each other's parameters or capital.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

AI_ACTORS = frozenset({"ai", "xingcheng", "model", "assistant"})


class RuntimeState:
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RISK_HALTED = "RISK_HALTED"
    DATA_BLOCKED = "DATA_BLOCKED"
    MODEL_BLOCKED = "MODEL_BLOCKED"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    ALL = frozenset({
        CREATED, READY, RUNNING, PAUSED, RISK_HALTED, DATA_BLOCKED,
        MODEL_BLOCKED, RECOVERING, STOPPED, FAILED})
    SAFETY = frozenset({RISK_HALTED})
    BLOCKED = frozenset({RISK_HALTED, DATA_BLOCKED, MODEL_BLOCKED})


_ALLOWED: dict[str, frozenset[str]] = {
    RuntimeState.CREATED: frozenset({RuntimeState.READY,
                                     RuntimeState.STOPPED}),
    RuntimeState.READY: frozenset({RuntimeState.RUNNING,
                                   RuntimeState.STOPPED}),
    RuntimeState.RUNNING: frozenset({
        RuntimeState.PAUSED, RuntimeState.RISK_HALTED,
        RuntimeState.DATA_BLOCKED, RuntimeState.MODEL_BLOCKED,
        RuntimeState.RECOVERING, RuntimeState.FAILED,
        RuntimeState.STOPPED}),
    RuntimeState.PAUSED: frozenset({RuntimeState.RUNNING,
                                    RuntimeState.STOPPED}),
    RuntimeState.RISK_HALTED: frozenset({RuntimeState.RECOVERING,
                                         RuntimeState.STOPPED}),
    RuntimeState.DATA_BLOCKED: frozenset({RuntimeState.RECOVERING,
                                          RuntimeState.STOPPED}),
    RuntimeState.MODEL_BLOCKED: frozenset({RuntimeState.RECOVERING,
                                           RuntimeState.STOPPED}),
    RuntimeState.RECOVERING: frozenset({
        RuntimeState.RUNNING, RuntimeState.PAUSED,
        RuntimeState.FAILED, RuntimeState.STOPPED}),
    RuntimeState.FAILED: frozenset({RuntimeState.RECOVERING,
                                    RuntimeState.STOPPED}),
    RuntimeState.STOPPED: frozenset(),
}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class MultiStrategyManager:
    """Isolated strategy runtimes — own params, own capital, own state."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "strategies.json"
        self._log = self._dir / "runtime-events.jsonl"
        self._runs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._runs = {r["run_id"]: r for r in json.loads(
                self._path.read_text(encoding="utf-8"))}
        except Exception:
            self._runs = {}

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            list(self._runs.values()), ensure_ascii=False, indent=1),
            encoding="utf-8")

    def _audit(self, run_id: str, kind: str,
               detail: dict[str, Any]) -> None:
        with open(self._log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"at": time.time(), "run_id": run_id, "kind": kind,
                 **detail}, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    def register(self, *, strategy_id: str, strategy_version: int,
                 market: str, instrument_scope: list[str],
                 strategy_type: str = "MOMENTUM",
                 parameters: dict[str, Any] | None = None,
                 execution_mode: str = "SHADOW",
                 account_id: str = "",
                 auto_recover: bool = False,
                 session_policy: dict[str, Any] | None = None,
                 ai_policy: str = "DETERMINISTIC") -> dict[str, Any]:
        if execution_mode not in ("SHADOW", "PAPER"):
            return {"ok": False, "error_code": "EXECUTION_MODE_UNKNOWN"}
        if ai_policy not in ("DETERMINISTIC", "AI_ASSISTED"):
            return {"ok": False, "error_code": "AI_POLICY_UNKNOWN"}
        run = {
            "run_id": _new_id("run"),
            "strategy_id": str(strategy_id),
            "strategy_version": int(strategy_version),
            "market": str(market),
            "instrument_scope": [str(i) for i in instrument_scope],
            "strategy_type": str(strategy_type),
            "parameters": dict(parameters or {}),
            "execution_mode": execution_mode,
            "account_id": str(account_id),
            "auto_recover": bool(auto_recover),
            "session_policy": dict(session_policy or {}),
            "ai_policy": ai_policy,
            "state": RuntimeState.CREATED,
            "state_reason": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "simulated": True,
        }
        self._runs[run["run_id"]] = run
        self._persist()
        self._audit(run["run_id"], "registered", {})
        return {"ok": True, "run": dict(run)}

    # ------------------------------------------------------------------
    def transition(self, run_id: str, target: str, *,
                   actor: str = "system", reason: str = ""
                   ) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            return {"ok": False, "error_code": "RUN_NOT_FOUND"}
        if target not in RuntimeState.ALL:
            return {"ok": False, "error_code": "STATE_UNKNOWN"}
        cur = run["state"]
        if target not in _ALLOWED.get(cur, frozenset()):
            return {"ok": False, "error_code": "ILLEGAL_TRANSITION",
                    "from": cur, "to": target}
        # AI can never lift a block/halt or start trading
        if actor.lower() in AI_ACTORS and target in (
                RuntimeState.RUNNING, RuntimeState.READY):
            return {"ok": False, "error_code": "AI_TRANSITION_DENIED"}
        # leaving a safety halt must go through RECOVERING — already
        # enforced by _ALLOWED; belt-and-suspenders: only the recovery
        # service (actor="recovery") or a human may move out of it
        if cur == RuntimeState.RISK_HALTED and actor.lower() in AI_ACTORS:
            return {"ok": False, "error_code": "AI_TRANSITION_DENIED"}
        run["state"] = target
        run["state_reason"] = reason
        run["updated_at"] = time.time()
        self._persist()
        self._audit(run_id, "transition",
                    {"from": cur, "to": target,
                     "actor": actor, "reason": reason})
        return {"ok": True, "run": dict(run)}

    # ------------------------------------------------------------------
    def update_config(self, run_id: str, patch: dict[str, Any], *,
                      actor: str = "user") -> dict[str, Any]:
        """Only the strategy's own config may be patched; a RUNNING
        strategy's parameters/capital cannot be mutated by AI, and
        cross-strategy writes are impossible by construction."""
        run = self._runs.get(run_id)
        if run is None:
            return {"ok": False, "error_code": "RUN_NOT_FOUND"}
        if actor.lower() in AI_ACTORS:
            return {"ok": False, "error_code": "AI_MUTATION_DENIED"}
        for k in ("parameters", "session_policy", "auto_recover",
                  "instrument_scope"):
            if k in patch:
                run[k] = patch[k]
        run["updated_at"] = time.time()
        self._persist()
        self._audit(run_id, "config_update",
                    {"keys": sorted(patch), "actor": actor})
        return {"ok": True, "run": dict(run)}

    def get(self, run_id: str) -> dict[str, Any] | None:
        r = self._runs.get(run_id)
        return dict(r) if r else None

    def by_strategy(self, strategy_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._runs.values()
                if r["strategy_id"] == strategy_id]

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._runs.values()]
        if state:
            rows = [r for r in rows if r["state"] == state]
        return rows

    def audit_trail(self, run_id: str | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:
        if not self._log.exists():
            return []
        out = []
        for line in self._log.read_text(
                encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if run_id is None or e.get("run_id") == run_id:
                out.append(e)
        return out[-limit:]
