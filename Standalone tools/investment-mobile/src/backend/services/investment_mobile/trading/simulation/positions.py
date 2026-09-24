"""PaperPositionService — virtual holdings from confirmed sim fills.

Positions change only through (a) confirmed PaperExecution rows or
(b) controlled corporate-action adjustments. No direct mutation API —
AI and callers post fills/events, never edit quantity fields.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import PaperExecution, PaperPosition


class PaperPositionService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "paper_positions.json"
        self._log_path = self._dir / "paper_position_events.jsonl"
        self._positions: dict[tuple[str, str], PaperPosition] = {}
        self._applied: set[str] = set()     # exec_ids — idempotent fills
        self._load()

    # ------------------------------------------------------------------
    def apply_fill(self, ex: PaperExecution) -> dict[str, Any]:
        if ex.exec_id in self._applied:
            return {"ok": True, "dedup": True}
        key = (ex.account_id, ex.instrument_id)
        pos = self._positions.get(key) or PaperPosition(
            account_id=ex.account_id, instrument_id=ex.instrument_id)
        qty, cost = pos.quantity, pos.average_cost
        if ex.side in ("buy", "subscribe"):
            total_cost = cost * qty + ex.price * ex.quantity + ex.fee
            pos.quantity = qty + ex.quantity
            pos.average_cost = (total_cost / pos.quantity
                                if pos.quantity > 0 else Decimal("0"))
        else:
            if ex.quantity > qty:
                return {"ok": False,
                        "error_code": "INSUFFICIENT_POSITION"}
            pos.realized_pnl += (ex.price - cost) * ex.quantity - ex.fee
            pos.quantity = qty - ex.quantity
            if pos.quantity <= 0:
                pos.quantity = Decimal("0")
                pos.average_cost = Decimal("0")
        pos.updated_at = time.time()
        self._positions[key] = pos
        self._applied.add(ex.exec_id)
        self._persist()
        self._log("fill", ex.account_id, ex.instrument_id,
                  {"exec_id": ex.exec_id, "qty": str(ex.quantity),
                   "price": str(ex.price)})
        return {"ok": True, "position": pos.to_dict()}

    def apply_corporate(self, account_id: str, instrument_id: str,
                        kind: str, ratio="1",
                        cash_amount="0") -> dict[str, Any]:
        """Controlled adjustment event — split/stock-dividend/cash."""
        key = (account_id, instrument_id)
        pos = self._positions.get(key)
        if pos is None or pos.quantity <= 0:
            return {"ok": False, "error_code": "NO_POSITION"}
        r = Decimal(str(ratio))
        if kind in ("split", "stock_dividend") and r > 0:
            pos.quantity *= r
            pos.average_cost /= r
        # cash dividends land in the cash ledger via caller (dividend row)
        pos.updated_at = time.time()
        self._persist()
        self._log("corporate", account_id, instrument_id,
                  {"kind": kind, "ratio": str(r),
                   "cash_amount": str(cash_amount)})
        return {"ok": True, "position": pos.to_dict()}

    def get(self, account_id: str, instrument_id: str
            ) -> PaperPosition | None:
        return self._positions.get((account_id, instrument_id))

    def list(self, account_id: str | None = None) -> list[dict[str, Any]]:
        rows = [p.to_dict() for p in self._positions.values()
                if p.quantity > 0]
        if account_id:
            rows = [r for r in rows if r["account_id"] == account_id]
        return rows

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._log_path.exists():
            return []
        return [json.loads(l) for l in
                self._log_path.read_text("utf-8").splitlines()
                if l.strip()][-limit:]

    # ------------------------------------------------------------------
    def _log(self, kind: str, account_id: str, instrument_id: str,
             detail: dict[str, Any]) -> None:
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "kind": kind, "account_id": account_id,
                "instrument_id": instrument_id, "detail": detail,
                "at": time.time(), "simulated": True},
                ensure_ascii=False) + "\n")

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except ValueError:
            return
        for r in data.get("positions", []):
            p = PaperPosition(
                account_id=r["account_id"], instrument_id=r["instrument_id"],
                quantity=r["quantity"], average_cost=r["average_cost"],
                realized_pnl=r.get("realized_pnl", "0"),
                currency=r.get("currency", ""))
            self._positions[(p.account_id, p.instrument_id)] = p
        self._applied = set(data.get("applied", []))

    def _persist(self) -> None:
        self._path.write_text(json.dumps({
            "positions": [p.to_dict() for p in self._positions.values()],
            "applied": sorted(self._applied)},
            ensure_ascii=False, indent=1), "utf-8")
