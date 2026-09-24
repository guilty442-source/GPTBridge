"""PaperRiskEngine — same rule contract as LIVE risk, sim-scoped.

Wraps the production RiskEngine.evaluate contract and adds paper-only
checks (strategy capital allocation, duplicate orders, stale market).
Outcomes: ALLOW | DENY | INCOMPLETE_EVIDENCE — insufficient data can
never pass. Every decision logs rule_version + reasons.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

RULE_VERSION = "paper-risk/v1"

_DEFAULT_LIMITS = {
    "max_order_notional": Decimal("1000000"),
    "max_position_notional": Decimal("2000000"),
    "max_strategy_capital": Decimal("5000000"),
    "max_daily_loss": Decimal("50000"),
    "max_drawdown_pct": Decimal("0.20"),
    "max_position_weight": Decimal("0.40"),
    "max_open_orders": 20,
    "max_quote_age_s": 172800.0,
}


class PaperRiskEngine:
    def __init__(self, state_dir: Path,
                 limits: dict[str, Any] | None = None) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "paper_risk_decisions.jsonl"
        merged = dict(_DEFAULT_LIMITS)
        if limits:
            merged.update({k: Decimal(str(v)) if not isinstance(v, int)
                           else v for k, v in limits.items()})
        self._limits = merged
        self._fp: Any | None = None

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    # ------------------------------------------------------------------
    def evaluate(
        self, *, account_id: str, instrument_id: str, side: str,
        quantity: Decimal, price: Decimal,
        strategy_run: dict[str, Any] | None,
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
        cash_available: Decimal,
        equity: Decimal,
        peak_equity: Decimal,
        daily_pnl: Decimal,
        quote_age_s: float | None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        incomplete: list[str] = []
        notional = quantity * price

        if price <= 0 or quantity <= 0:
            incomplete.append("no_reference_price")
        if quote_age_s is None:
            incomplete.append("no_market_timestamp")
        elif quote_age_s > float(self._limits["max_quote_age_s"]):
            reasons.append("stale_market_data")

        if notional > self._limits["max_order_notional"]:
            reasons.append("order_notional_exceeds_limit")

        held = next((p for p in positions
                     if p["instrument_id"] == instrument_id), None)
        held_val = (Decimal(str(held["quantity"]))
                    * price if held else Decimal("0"))
        proj = held_val + (notional if side in ("buy", "subscribe")
                           else -notional)
        if proj > self._limits["max_position_notional"]:
            reasons.append("position_notional_exceeds_limit")
        if equity > 0 and proj / equity > \
                self._limits["max_position_weight"]:
            reasons.append("position_weight_exceeds_limit")

        if strategy_run is not None:
            cap = Decimal(str(strategy_run.get("allocated_capital") or 0))
            if side in ("buy", "subscribe") and notional > cap:
                reasons.append("strategy_capital_exceeded")

        if len(open_orders) >= int(self._limits["max_open_orders"]):
            reasons.append("too_many_open_orders")
        if any(o["instrument_id"] == instrument_id
               and o["side"] == side for o in open_orders):
            reasons.append("duplicate_open_order")

        if daily_pnl <= -self._limits["max_daily_loss"]:
            reasons.append("daily_loss_limit_breached")
        if peak_equity > 0:
            dd = (peak_equity - equity) / peak_equity
            if dd > self._limits["max_drawdown_pct"]:
                reasons.append("max_drawdown_breached")

        if side in ("buy", "subscribe") and notional > cash_available:
            reasons.append("insufficient_cash")

        outcome = ("DENY" if reasons else
                   "INCOMPLETE_EVIDENCE" if incomplete else "ALLOW")
        rec = {
            "decision_id": f"prisk-{uuid.uuid4().hex[:12]}",
            "rule_version": RULE_VERSION, "outcome": outcome,
            "reasons": reasons, "incomplete": incomplete,
            "account_id": account_id, "instrument_id": instrument_id,
            "side": side, "notional": str(notional), "at": time.time(),
            "simulated": True,
        }
        if self._fp is None:
            self._fp = self._path.open("a", encoding="utf-8")
        self._fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fp.flush()
        return rec

    def decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [json.loads(l) for l in
                self._path.read_text("utf-8").splitlines()
                if l.strip()][-limit:]
