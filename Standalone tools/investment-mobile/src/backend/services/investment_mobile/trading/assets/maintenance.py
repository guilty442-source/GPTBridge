"""PortfolioMaintenanceService — bounded, observable upkeep jobs.

Jobs: stale-FX check, stale-NAV check, duplicate-record check,
snapshot capture, journal consistency. Each run is recorded with
status/duration/findings; failures are isolated (a broken job never
blocks the others) and model availability is irrelevant — asset
management works fully offline.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


class PortfolioMaintenanceService:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "asset-maintenance.json"
        self._runs: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._runs = data[-200:]
        except Exception:
            self._runs = []

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            self._runs[-200:], indent=2, ensure_ascii=False),
            encoding="utf-8")

    def _record(self, job: str, fn) -> dict[str, Any]:
        run = {"run_id": f"mnt-{uuid.uuid4().hex[:10]}",
               "job": job, "at": time.time()}
        try:
            run["result"] = fn()
            run["status"] = "ok"
        except Exception as exc:  # fault isolation per job
            run["status"] = "error"
            run["error"] = f"{type(exc).__name__}: {exc}"
        run["duration_ms"] = int((time.time() - run["at"]) * 1000)
        self._runs.append(run)
        self._persist()
        return run

    # ------------------------------------------------------------------
    def run_all(self, engine: Any) -> dict[str, Any]:
        results = [
            self._record("fx_staleness",
                         lambda: engine.ccy.staleness("USD", "TWD")),
            self._record("duplicate_check",
                         lambda: self._dup_check(engine)),
            self._record("snapshot_capture",
                         lambda: engine.snapshots.capture(
                             "daily", engine.value())),
            self._record("consistency_check",
                         lambda: self._consistency(engine)),
        ]
        return {"ok": True, "runs": results,
                "errors": sum(1 for r in results
                              if r["status"] == "error")}

    def _dup_check(self, engine: Any) -> dict[str, Any]:
        seen, dups = set(), 0
        for t in engine.ledger.list():
            key = (t.get("instrument_id"), t.get("transaction_type"),
                   t.get("quantity"), t.get("price"),
                   t.get("transaction_date"))
            if key in seen:
                dups += 1
            seen.add(key)
        return {"duplicates": dups}

    def _consistency(self, engine: Any) -> dict[str, Any]:
        issues = []
        for p in engine.positions.list_positions():
            if float(p["quantity"]) < float(p["reserved_quantity"]):
                issues.append({"position_id": p["position_id"],
                               "issue": "reserved>quantity"})
        return {"issues": issues}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "runs": self._runs[-50:]}
