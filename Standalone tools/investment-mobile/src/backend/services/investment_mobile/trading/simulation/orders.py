"""PaperOrderManagementSystem — simulated order book.

Strict state machine, client_order_id idempotency, no double-fill:
fills accumulate against open_qty and terminal states reject all
further actions. This book never calls broker adapters.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import PaperExecution, PaperOrder, PaperOrderStatus


class PaperOrderManagementSystem:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._orders_path = self._dir / "paper_orders.jsonl"
        self._execs_path = self._dir / "paper_executions.jsonl"
        self._orders: dict[str, PaperOrder] = {}
        self._by_client: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    def submit(self, order: PaperOrder) -> dict[str, Any]:
        """CREATED → VALIDATED → ACCEPTED (risk/cash gates upstream)."""
        if order.client_order_id:
            existing = self._by_client.get(order.client_order_id)
            if existing is not None:
                return {"ok": True, "dedup": True,
                        "order": self._orders[existing].to_dict()}
        if order.quantity <= 0:
            order.status = PaperOrderStatus.REJECTED
            self._record(order)
            return {"ok": False, "error_code": "QUANTITY_INVALID"}
        order.status = PaperOrderStatus.VALIDATED
        order.status = PaperOrderStatus.ACCEPTED
        self._orders[order.order_id] = order
        if order.client_order_id:
            self._by_client[order.client_order_id] = order.order_id
        self._record(order)
        return {"ok": True, "order": order.to_dict()}

    def apply_fill(self, order_id: str, ex: PaperExecution
                   ) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if order.status in PaperOrderStatus.TERMINAL:
            return {"ok": False, "error_code": "ORDER_TERMINAL",
                    "status": order.status}
        if ex.quantity <= 0 or ex.quantity > order.open_qty:
            return {"ok": False, "error_code": "OVERFILL"}
        prev_val = order.avg_fill_price * order.filled_qty
        order.filled_qty += ex.quantity
        order.avg_fill_price = (
            (prev_val + ex.price * ex.quantity) / order.filled_qty)
        order.status = (PaperOrderStatus.FILLED if order.open_qty == 0
                        else PaperOrderStatus.PARTIALLY_FILLED)
        self._record(order)
        self._append_exec(ex.to_dict())
        return {"ok": True, "order": order.to_dict(),
                "execution": ex.to_dict()}

    def cancel(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if not order.can_transition(PaperOrderStatus.CANCELLED):
            return {"ok": False, "error_code": "CANNOT_CANCEL",
                    "status": order.status}
        order.status = PaperOrderStatus.CANCELLED
        self._record(order)
        return {"ok": True, "order": order.to_dict()}

    def expire_due(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        expired = []
        for o in self._orders.values():
            if (o.expires_at and o.expires_at <= now
                    and o.status in (PaperOrderStatus.ACCEPTED,
                                     PaperOrderStatus.PARTIALLY_FILLED)):
                o.status = PaperOrderStatus.EXPIRED
                self._record(o)
                expired.append(o.order_id)
        return {"ok": True, "expired": expired}

    def get(self, order_id: str) -> dict[str, Any] | None:
        o = self._orders.get(order_id)
        return o.to_dict() if o else None

    def list(self, account_id: str | None = None,
             status: str | None = None) -> list[dict[str, Any]]:
        rows = [o.to_dict() for o in self._orders.values()]
        if account_id:
            rows = [r for r in rows if r["account_id"] == account_id]
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows

    def open_orders(self) -> list[PaperOrder]:
        return [o for o in self._orders.values()
                if o.status in (PaperOrderStatus.ACCEPTED,
                                PaperOrderStatus.PARTIALLY_FILLED)]

    def executions(self, order_id: str | None = None
                   ) -> list[dict[str, Any]]:
        if not self._execs_path.exists():
            return []
        rows = [json.loads(l) for l in
                self._execs_path.read_text("utf-8").splitlines()
                if l.strip()]
        if order_id:
            rows = [r for r in rows if r["order_id"] == order_id]
        return rows

    # ------------------------------------------------------------------
    def _record(self, order: PaperOrder) -> None:
        with self._orders_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(order.to_dict(), ensure_ascii=False)
                     + "\n")

    def _append_exec(self, row: dict[str, Any]) -> None:
        with self._execs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        if not self._orders_path.exists():
            return
        for line in self._orders_path.read_text("utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            o = PaperOrder(
                account_id=r["account_id"], instrument_id=r["instrument_id"],
                side=r["side"], quantity=r["quantity"],
                order_type=r.get("order_type", "market"),
                strategy_id=r.get("strategy_id", ""),
                strategy_version=int(r.get("strategy_version") or 0),
                limit_price=r.get("limit_price"),
                reference_price=r.get("reference_price"),
                currency=r.get("currency", ""),
                expires_at=float(r.get("expires_at") or 0),
                client_order_id=r.get("client_order_id", ""),
                order_id=r["order_id"], status=r["status"],
                filled_qty=r.get("filled_qty", "0"),
                avg_fill_price=r.get("avg_fill_price", "0"),
                created_at=float(r.get("created_at") or 0))
            self._orders[o.order_id] = o
            if o.client_order_id:
                self._by_client[o.client_order_id] = o.order_id
