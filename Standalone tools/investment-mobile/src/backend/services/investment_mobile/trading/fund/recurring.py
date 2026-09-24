"""FundRecurringInvestmentService — 定期定額 plans + analysis.

Plans and their performance are analyzed here; the service only emits
ADVICE (maintain / adjust / pause / resume / review-alternative). It
never touches bank or fund-platform debit settings — those remain
operator actions on the platform side.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .cost import FundCostBasisService
from .contracts import FundTransactionType
from .performance import FundPerformanceEngine
from .transactions import FundTransactionService


class FundRecurringInvestmentService:
    def __init__(
        self,
        state_dir: Path,
        transactions: FundTransactionService,
        cost: FundCostBasisService,
        performance: FundPerformanceEngine,
    ) -> None:
        self._path = Path(state_dir) / "fund-recurring.json"
        self._txns = transactions
        self._cost = cost
        self._perf = performance

    # ------------------------------------------------------------------
    def register_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = {
            "plan_id": f"plan-{uuid.uuid4().hex[:12]}",
            "account_id": str(payload.get("account_id") or ""),
            "fund_id": str(payload.get("fund_id") or ""),
            "share_class_id": str(payload.get("share_class_id") or ""),
            "amount": str(payload.get("amount") or "0"),
            "currency": str(payload.get("currency") or "TWD"),
            "day_of_month": int(payload.get("day_of_month") or 6),
            "status": "active",
        }
        plans = self._plans()
        plans.append(plan)
        self._persist(plans)
        return {"ok": True, "plan": plan}

    def set_status(self, plan_id: str, status: str) -> dict[str, Any]:
        """Advice-level status only — actual bank debit unchanged."""
        if status not in ("active", "paused"):
            return {"ok": False, "error_code": "STATUS_UNKNOWN"}
        plans = self._plans()
        plan = next((p for p in plans if p["plan_id"] == plan_id), None)
        if plan is None:
            return {"ok": False, "error_code": "PLAN_UNKNOWN"}
        plan["status"] = status
        self._persist(plans)
        return {"ok": True, "plan": plan,
                "note": "僅記錄建議狀態——平台扣款設定需人工異動"}

    # ------------------------------------------------------------------
    def analyze(self, plan_id: str) -> dict[str, Any]:
        plan = next((p for p in self._plans() if p["plan_id"] == plan_id),
                    None)
        if plan is None:
            return {"ok": False, "error_code": "PLAN_UNKNOWN"}
        basis = self._cost.basis(
            plan["account_id"], plan["fund_id"], plan["share_class_id"])
        invested_txns = [
            t for t in self._txns.settled(
                fund_id=plan["fund_id"], account_id=plan["account_id"])
            if t.transaction_type == FundTransactionType.RECURRING.value
            and t.share_class_id == plan["share_class_id"]
        ]
        invested = sum((t.amount for t in invested_txns), Decimal("0"))
        perf = self._perf.period_return(
            plan["fund_id"], plan["share_class_id"], "1y")
        advice = self._advise(plan, basis, perf)
        return {
            "ok": True, "plan": plan,
            "recurring_installments": len(invested_txns),
            "recurring_invested": str(invested),
            "basis": basis,
            "period_return_1y": perf,
            "advice": advice,
            "advice_only": True,
        }

    @staticmethod
    def _advise(
        plan: dict[str, Any], basis: dict[str, Any],
        perf: dict[str, Any],
    ) -> dict[str, Any]:
        if not perf.get("ok"):
            return {"action": "maintain",
                    "reason": "insufficient performance data"}
        ret = Decimal(str(perf.get("return") or "0"))
        unreal = basis.get("unrealized_pnl")
        if ret < Decimal("-0.15"):
            return {
                "action": "review",
                "reason": "一年期跌幅超過 15%——建議檢視替代基金或調整金額",
                "options": ["adjust_amount", "pause", "review_alternative"],
            }
        return {"action": "maintain",
                "reason": "performance within normal range"}

    # ------------------------------------------------------------------
    def _plans(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _persist(self, plans: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(plans, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self._path)

    def list_plans(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return [
            p for p in self._plans()
            if account_id is None or p["account_id"] == account_id
        ]
