from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def coordinate_investment_accounting(payload: dict[str, Any]) -> dict[str, Any]:
    """Let Star decide whether a computed ledger reconciliation is safe to apply.

    Star never receives database access. The investment manager supplies a
    minimal reconciliation snapshot and remains the only database writer.
    """

    reconciliation = payload.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return {
            "ok": False,
            "error_code": "INVALID_ACCOUNTING_SNAPSHOT",
            "message": "缺少可驗證的帳務對帳快照。",
        }
    differences = [
        item
        for item in reconciliation.get("differences", [])
        if isinstance(item, dict)
    ][:100]
    safe_actions: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for item in differences:
        suggestion = item.get("suggestion")
        if not isinstance(suggestion, dict):
            rejected.append({"symbol": str(item.get("symbol") or ""), "reason": "missing_suggestion"})
            continue
        symbol = str(suggestion.get("symbol") or "").strip().upper()
        side = str(suggestion.get("side") or "").strip().upper()
        quantity = _number(suggestion.get("quantity"))
        price = _number(suggestion.get("price"))
        if not symbol or side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0:
            rejected.append({"symbol": symbol, "reason": "unsafe_estimate"})
            continue
        safe_actions.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "currency": str(suggestion.get("currency") or "TWD").upper(),
            }
        )

    autonomous = payload.get("autonomous") is True
    apply_reconciliation = bool(differences) and not rejected and autonomous
    decision = (
        "no_action"
        if not differences
        else "apply_estimated_reconciliation"
        if apply_reconciliation
        else "manual_review"
    )
    return {
        "ok": True,
        "accounting_owner": "星澄",
        "decision": decision,
        "apply_reconciliation": apply_reconciliation,
        "reviewed_difference_count": len(differences),
        "approved_action_count": len(safe_actions),
        "approved_actions": safe_actions,
        "rejected": rejected,
        "autonomous": autonomous,
        "network_used": False,
        "database_access": False,
        "database_writer": "ai-assistant",
        "facts_locked": True,
        "message": (
            "星澄已核准建立估算對帳調整。"
            if apply_reconciliation
            else "帳務已相符，不需調整。"
            if not differences
            else "星澄偵測到不安全的帳務差異，已停止自動寫入。"
        ),
    }
