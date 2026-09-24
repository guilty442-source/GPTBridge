"""TradingPersistenceService — durable-before-dispatch journals.

Rule: every important trading state lands on disk BEFORE the external
action it authorizes (intent before submission, submission before
report). A broker call and a PostgreSQL row are not one atomic unit —
recovery reconciles journals instead of pretending otherwise.

All journals are append-only JSONL with persistent handles (single
writer process); ``recover()`` reconstructs the in-flight set on boot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_JOURNALS = (
    "live_intents",
    "live_orders",
    "live_order_events",
    "live_submissions",
    "live_broker_reports",
    "live_executions",
    "live_risk_decisions",
    "live_authorizations",
    "live_account_snapshots",
    "live_reconciliations",
    "live_emergency_events",
    "live_audit",
)


class TradingPersistenceService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "live"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fps: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def _w(self, journal: str) -> Any:
        fp = self._fps.get(journal)
        if fp is None:
            if journal not in _JOURNALS:
                raise ValueError(f"JOURNAL_UNKNOWN:{journal}")
            fp = (self._dir / f"{journal}.jsonl").open("a",
                                                     encoding="utf-8")
            self._fps[journal] = fp
        return fp

    def record(self, journal: str, row: dict[str, Any]) -> None:
        fp = self._w(journal)
        fp.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        fp.flush()

    def read(self, journal: str) -> list[dict[str, Any]]:
        path = self._dir / f"{journal}.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def tail(self, journal: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.read(journal)[-max(1, int(limit)):]

    def close(self) -> None:
        for fp in self._fps.values():
            fp.close()
        self._fps.clear()

    # ------------------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        """Boot-time in-flight summary — never re-dispatch blindly.

        - ``pending_intents``: intents whose order never reached SUBMITTED
          or beyond — candidates for review, not resubmission.
        - ``unknown_submissions``: orders with a submission record but no
          matching broker ack — must reconcile with the broker first.
        - ``open_orders``: orders in a non-terminal state.
        """
        orders: dict[str, dict[str, Any]] = {}
        for row in self.read("live_orders"):
            orders[row.get("internal_order_id") or row.get("order_id")
                   or ""] = row
        submitted_ids = {
            r.get("order_id")
            for r in self.read("live_submissions")
            if r.get("outcome") in ("ack", "reject")
        }
        unknown_ids = {
            r.get("order_id")
            for r in self.read("live_submissions")
            if r.get("outcome") in ("timeout", "unknown")
        }
        terminal = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        open_orders = [o for o in orders.values()
                       if o.get("state") not in terminal]
        pending_intents = [
            o for o in open_orders
            if o.get("internal_order_id") not in submitted_ids
            and o.get("internal_order_id") not in unknown_ids
            and o.get("state") in ("CREATED", "VALIDATING", "AUTHORIZED",
                                   "RISK_APPROVED", "SUBMISSION_PENDING")
        ]
        unknown_submissions = [
            o for o in open_orders
            if o.get("internal_order_id") in unknown_ids
            or o.get("state") == "SUBMISSION_UNKNOWN"
        ]
        return {
            "orders_total": len(orders),
            "open_orders": open_orders,
            "pending_intents": pending_intents,
            "unknown_submissions": unknown_submissions,
        }
