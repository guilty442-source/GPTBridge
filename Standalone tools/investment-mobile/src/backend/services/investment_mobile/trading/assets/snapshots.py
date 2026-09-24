"""PortfolioSnapshotService — versioned daily/weekly/monthly snapshots.

Snapshots are append-only versions. Re-imported history produces a NEW
version (``version`` increments, ``supersedes`` links back) — history is
never silently overwritten.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

PERIODS = ("daily", "weekly", "monthly")


class PortfolioSnapshotService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "asset-snapshots"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "snapshots.jsonl"
        self._fh = open(self._path, "a", encoding="utf-8")
        self._snaps: list[dict[str, Any]] = []
        self._replay()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                self._snaps.append(json.loads(line))
            except Exception:
                continue

    # ------------------------------------------------------------------
    def capture(self, period: str, valuation: dict[str, Any], *,
                data_version: str = "") -> dict[str, Any]:
        if period not in PERIODS:
            return {"ok": False, "error_code": "PERIOD_UNKNOWN",
                    "periods": list(PERIODS)}
        day = int(time.time() // 86400)
        seq = sum(1 for s in self._snaps
                  if s["period"] == period and s["day"] == day) + 1
        snap = {
            "snapshot_id": f"snap-{uuid.uuid4().hex[:10]}",
            "period": period, "day": day, "version": seq,
            "supersedes": (seq - 1) or None,
            "at": time.time(),
            "data_version": data_version,
            "total_assets": valuation.get("total_assets"),
            "per_account": valuation.get("per_account"),
            "per_market": valuation.get("per_market"),
            "per_currency": valuation.get("per_currency"),
            "positions": valuation.get("positions"),
            "cash_detail": valuation.get("cash_detail"),
            "valuation_times": valuation.get("valuation_times"),
            "fx_rates": valuation.get("fx_rates", []),
            "stale_data": valuation.get("stale_data", False),
        }
        self._fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._snaps.append(snap)
        return {"ok": True, "snapshot_id": snap["snapshot_id"],
                "version": seq}

    def history(self, period: str = "daily",
                limit: int = 400) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items()
                 if k in ("snapshot_id", "period", "day", "version",
                          "at", "total_assets", "stale_data")}
                for s in self._snaps if s["period"] == period][-limit:]

    def curve(self, period: str = "daily") -> list[dict[str, Any]]:
        return [{"at": s["at"], "value": s["total_assets"]}
                for s in self._snaps if s["period"] == period]
