"""StrategyRegistry + StrategyVersionService.

Registration, parameter management, lifecycle transitions and immutable
version snapshots. A new version never mutates a released one; history
replays against the same version + data revision reproduce identically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import (
    StrategyDefinition, StrategyStatus, StrategyVersionSnapshot,
    _ALLOWED,
)


class StrategyRegistry:
    """Definitions + lifecycle. Persistence: strategy_defs.json"""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "strategy_defs.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._defs: dict[str, StrategyDefinition] = {}
        self._load()

    # ------------------------------------------------------------------
    def register(self, definition: StrategyDefinition) -> dict[str, Any]:
        errors = definition.validate()
        if errors:
            return {"ok": False, "errors": errors}
        existing = self._defs.get(definition.strategy_id)
        if existing is not None and existing.version >= definition.version:
            # same id → must bump version; never silently overwrite
            if existing.to_dict() != definition.to_dict():
                return {"ok": False,
                        "error_code": "VERSION_BUMP_REQUIRED",
                        "current_version": existing.version}
        self._defs[definition.strategy_id] = definition
        self._persist()
        return {"ok": True, "strategy": definition.to_dict()}

    def get(self, strategy_id: str,
            version: int | None = None) -> dict[str, Any] | None:
        d = self._defs.get(strategy_id)
        if d is None:
            return None
        if version is not None and d.version != version:
            return None
        return d.to_dict()

    def list(self, status: str | None = None,
             market: str | None = None) -> list[dict[str, Any]]:
        rows = [d.to_dict() for d in self._defs.values()]
        if status:
            rows = [r for r in rows if r["status"] == status]
        if market:
            rows = [r for r in rows if r["market"] == market]
        return rows

    def transition(self, strategy_id: str, target: str,
                   reason: str = "") -> dict[str, Any]:
        d = self._defs.get(strategy_id)
        if d is None:
            return {"ok": False, "error_code": "STRATEGY_NOT_FOUND"}
        if target not in StrategyStatus.ALL:
            return {"ok": False, "error_code": "STATUS_UNKNOWN"}
        if target not in _ALLOWED.get(d.status, frozenset()):
            return {"ok": False, "error_code": "ILLEGAL_TRANSITION",
                    "from": d.status, "to": target}
        d.status = target
        self._persist()
        return {"ok": True, "strategy_id": strategy_id, "status": target}

    def disable(self, strategy_id: str, reason: str = "") -> dict[str, Any]:
        d = self._defs.get(strategy_id)
        if d is None:
            return {"ok": False, "error_code": "STRATEGY_NOT_FOUND"}
        d.status = StrategyStatus.RETIRED
        self._persist()
        return {"ok": True, "status": StrategyStatus.RETIRED,
                "reason": reason}

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text("utf-8"))
        except ValueError:
            return
        for row in rows:
            d = StrategyDefinition.from_dict(row)
            self._defs[d.strategy_id] = d

    def _persist(self) -> None:
        rows = [d.to_dict() for d in self._defs.values()]
        self._path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")


class StrategyVersionService:
    """Immutable version snapshots — append-only."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "strategy_versions.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def snapshot(self, definition: StrategyDefinition, *,
                 code_version: str = "", data_version: str = "",
                 model_version: str = "",
                 cost_assumptions: dict[str, Any] | None = None
                 ) -> dict[str, Any]:
        snap = StrategyVersionSnapshot(
            strategy_id=definition.strategy_id, version=definition.version,
            definition=definition.to_dict(), code_version=code_version,
            data_version=data_version, model_version=model_version,
            risk_params=dict(definition.risk_profile),
            cost_assumptions=dict(cost_assumptions or {}))
        self._append(snap.to_dict())
        return {"ok": True, "snapshot": snap.to_dict()}

    def attach_backtest(self, strategy_id: str, version: int,
                        run_id: str) -> None:
        # snapshots immutable → linkage recorded as separate row
        self._append({"link": "backtest", "strategy_id": strategy_id,
                      "version": int(version), "run_id": run_id})

    def attach_validation(self, strategy_id: str, version: int,
                          result: dict[str, Any]) -> None:
        self._append({"link": "validation", "strategy_id": strategy_id,
                      "version": int(version), "result": result})

    def versions(self, strategy_id: str | None = None
                 ) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("snapshot_id") is None:
                continue
            if strategy_id and row.get("strategy_id") != strategy_id:
                continue
            out.append(row)
        return out

    def _append(self, row: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
