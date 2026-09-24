"""LiveAccountService + AccountReconciliationService.

Broker-reported state is the external fact source. PostgreSQL/JSONL
hold: fetched broker snapshots, internal intents, reservations,
reconciliation results, audit rows. Local projections never overwrite
broker truth — divergence flags the account RECONCILIATION_REQUIRED and
stops new risk on it until an authorized resume.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import ReconciliationReport, ReconciliationResult

_D = Decimal
_RESUME_ISSUERS = frozenset({"governor", "user", "risk-officer"})
_AI_ACTORS = frozenset({
    "ai", "xingcheng", "model", "llm", "assistant", "agent",
    "local-model", "星澄"})


class LiveAccountService:
    """Live account state mirror — broker snapshot is authoritative."""

    def __init__(self, journal) -> None:
        self._journal = journal           # persistence.record
        self._snapshots: dict[str, dict[str, Any]] = {}

    def ingest_broker_snapshot(self, account_id: str,
                               snapshot: dict[str, Any]) -> dict[str, Any]:
        """Store broker-reported truth verbatim (as external evidence)."""
        row = {
            "account_id": str(account_id),
            "source": "broker",
            "balances": dict(snapshot.get("balances") or {}),
            "positions": list(snapshot.get("positions") or []),
            "open_orders": list(snapshot.get("open_orders") or []),
            "at": time.time(),
            "snapshot_id": f"snap-{account_id}-{int(time.time())}",
        }
        self._snapshots[str(account_id)] = row
        self._journal("live_account_snapshots", row)
        return {"ok": True, "snapshot_id": row["snapshot_id"]}

    def broker_state(self, account_id: str) -> dict[str, Any] | None:
        return self._snapshots.get(str(account_id))


class AccountReconciliationService:
    """Local-vs-broker comparison; mismatch halts new opens."""

    def __init__(self, state_dir: Path, journal) -> None:
        self._path = Path(state_dir) / "live_reconciliation_halt.json"
        self._journal = journal
        self._halted: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._halted = data
        except Exception:
            self._halted = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._halted, indent=1,
                                  ensure_ascii=False), "utf-8")
        tmp.replace(self._path)

    def halted(self, account_id: str) -> bool:
        return str(account_id) in self._halted

    def halt_reason(self, account_id: str) -> dict[str, Any] | None:
        return self._halted.get(str(account_id))

    # ------------------------------------------------------------------
    def reconcile(self, account_id: str, *,
                  local_orders: list[dict[str, Any]],
                  local_executions: list[dict[str, Any]],
                  local_positions: list[dict[str, Any]],
                  local_cash: dict[str, Any],
                  broker_snapshot: dict[str, Any] | None,
                  ) -> dict[str, Any]:
        """Compare local journals against broker-reported truth."""
        account_id = str(account_id)
        if broker_snapshot is None:
            report = ReconciliationReport(
                account_id=account_id,
                result=ReconciliationResult.UNAVAILABLE.value,
                mismatches=[{"what": "broker_snapshot_missing"}])
            self._journal("live_reconciliations", report.to_dict())
            return {"ok": False, "report": report.to_dict()}

        mismatches: list[dict[str, Any]] = []
        checked = ["orders", "executions", "positions", "cash"]

        # positions: instrument → quantity
        local_pos = {str(p.get("instrument_id")):
                     _D(str(p.get("quantity") or 0))
                     for p in local_positions}
        remote_pos = {str(p.get("instrument_id")):
                      _D(str(p.get("quantity") or 0))
                      for p in broker_snapshot.get("positions") or []}
        for iid in set(local_pos) | set(remote_pos):
            if local_pos.get(iid, _D("0")) != remote_pos.get(iid, _D("0")):
                mismatches.append({
                    "what": "position", "instrument_id": iid,
                    "local": str(local_pos.get(iid, 0)),
                    "remote": str(remote_pos.get(iid, 0))})

        # cash
        remote_cash = broker_snapshot.get("balances") or {}
        for ccy in set(local_cash) | set(remote_cash):
            la = _D(str((local_cash.get(ccy) or {}).get("available", 0)))
            ra = _D(str((remote_cash.get(ccy) or {}).get("available", 0)))
            if la != ra:
                mismatches.append({
                    "what": "cash", "currency": ccy,
                    "local": str(la), "remote": str(ra)})

        # orders: broker may hold orders we never saw (SUBMISSION_UNKNOWN)
        remote_open = {
            str(o.get("client_order_key") or o.get("broker_order_id"))
            for o in broker_snapshot.get("open_orders") or []}
        local_keys = {
            str(o.get("client_order_key")) for o in local_orders}
        unknown_remote = remote_open - local_keys
        for key in sorted(unknown_remote):
            mismatches.append({"what": "unmatched_remote_order",
                               "client_order_key": key})

        result = (ReconciliationResult.MATCHED.value if not mismatches
                  else ReconciliationResult.MISMATCHED.value)
        report = ReconciliationReport(
            account_id=account_id, result=result,
            checked=checked, mismatches=mismatches,
            local_revision=str(len(local_orders)),
            remote_revision=str(
                len(broker_snapshot.get("open_orders") or [])))
        self._journal("live_reconciliations", report.to_dict())
        if mismatches:
            self._halted[account_id] = {
                "reason": "reconciliation_mismatch",
                "report_id": report.report_id, "at": time.time()}
            self._persist()
        return {"ok": True, "report": report.to_dict()}

    def resume(self, account_id: str, by: str,
               evidence: str = "") -> dict[str, Any]:
        """Authorized release of a reconciliation halt — never AI."""
        if str(by).lower() in _AI_ACTORS:
            return {"ok": False, "error_code": "AI_CANNOT_RELEASE"}
        if str(by) not in _RESUME_ISSUERS:
            return {"ok": False, "error_code": "RESUME_NOT_AUTHORIZED"}
        if str(account_id) not in self._halted:
            return {"ok": False, "error_code": "ACCOUNT_NOT_HALTED"}
        del self._halted[str(account_id)]
        self._persist()
        return {"ok": True, "account_id": account_id,
                "resumed_by": by, "evidence": evidence}
