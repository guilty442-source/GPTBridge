"""FundStrategyEngine — versioned fund strategies.

Strategies evaluate allocation / recurring / rebalance / risk / switch /
tracking logic against verifiable inputs (classification, performance,
fees, holdings, cost basis). Every version is retained — superseded
strategies stay auditable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import FundStrategy


class FundStrategyEngine:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-strategies.jsonl"

    # ------------------------------------------------------------------
    def register(self, strategy: FundStrategy) -> dict[str, Any]:
        # retire earlier versions of the same strategy_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(strategy.to_dict(), ensure_ascii=False) + "\n")
        return {"ok": True, "strategy": strategy.to_dict()}

    def versions(self, strategy_id: str) -> list[dict[str, Any]]:
        return [
            s for s in self._all()
            if s["strategy_id"] == strategy_id
        ]

    def active(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for s in self._all():
            latest[s["strategy_id"]] = s   # append order = version order
        out = [s for s in latest.values() if s["status"] == "active"]
        if strategy_id:
            out = [s for s in out if s["strategy_id"] == strategy_id]
        return out

    def retire(self, strategy_id: str) -> dict[str, Any]:
        rows = self._all()
        found = False
        for s in rows:
            if s["strategy_id"] == strategy_id and s["status"] == "active":
                s["status"] = "retired"
                found = True
        if not found:
            return {"ok": False, "error_code": "STRATEGY_UNKNOWN"}
        self._rewrite(rows)
        return {"ok": True, "strategy_id": strategy_id}

    # ------------------------------------------------------------------
    def _all(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        return [
            json.loads(l) for l in
            self._path.read_text(encoding="utf-8").splitlines() if l
        ]

    def _rewrite(self, rows: list[dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text("\n".join(
            json.dumps(r, ensure_ascii=False) for r in rows
        ) + ("\n" if rows else ""), encoding="utf-8")
        tmp.replace(self._path)
