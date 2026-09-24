"""PortfolioReallocationService — candidate plans, never orders.

Each proposal carries: before-allocation, candidate after-allocation,
estimated transaction cost, estimated risk change, data sources and the
assumptions used. AI may draft; it can never mutate the user's stored
targets (allocation engine + schema both deny).
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PortfolioReallocationService:
    def __init__(self, state_dir: Path, allocation: Any,
                 cost_estimator: Any | None = None) -> None:
        self._path = state_dir / "monitoring" / "reallocation.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._alloc = allocation
        self._cost = cost_estimator
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
    def propose(self, valuation: dict[str, Any],
                *, dimension: str = "market",
                tags: dict[str, dict[str, str]] | None = None,
                actor: str = "user") -> dict[str, Any]:
        analysis = self._alloc.analyze(valuation, tags)
        if not analysis.get("ok"):
            return analysis
        total = _d(analysis["total_assets"])
        targets = analysis["targets"].get(dimension, {})
        before = analysis["weights"].get(dimension, {})
        moves = []
        after = dict(before)
        for key, t in targets.items():
            tw = _d(t["weight"])
            cur = _d(before.get(key, "0"))
            drift = cur - tw
            if abs(drift) <= _d(t["drift_band"]):
                continue
            delta_value = drift * total
            cost = None
            if self._cost is not None:
                try:
                    cost = self._cost.estimate(
                        {"market": key, "side": "sell" if drift > 0
                         else "buy",
                         "notional": str(abs(delta_value))})
                except Exception:
                    cost = None
            moves.append({"key": key, "action": "reduce" if drift > 0
                          else "increase",
                          "delta_weight": str(-drift),
                          "delta_value": str(-delta_value),
                          "estimated_cost": cost})
            after[key] = str(tw)
        proposal = {
            "proposal_id": f"ral-{uuid.uuid4().hex[:10]}",
            "dimension": dimension,
            "created_at": time.time(),
            "actor": actor,
            "before": before,
            "after": after,
            "moves": moves,
            "estimated_total_cost": sum(
                _d((m.get("estimated_cost") or {}).get("total", "0"))
                for m in moves),
            "risk_change": "concentration reduced toward targets"
                           if moves else "none",
            "data_sources": ["offline-accounts", "valuation",
                             "allocation-targets"],
            "assumptions": ["latest valuation prices",
                            "no market impact",
                            "fractional fund units allowed"],
            "status": "candidate",
            "advisory": True,
        }
        proposal["estimated_total_cost"] = str(
            proposal["estimated_total_cost"])
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(proposal, ensure_ascii=False) + "\n")
        self._proposals.append(proposal)
        return {"ok": True, "proposal": proposal,
                "note": "candidate only — never mutates user targets "
                        "or creates orders"}

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._proposals[-limit:]
