"""MonitoringEvent contract + dedup-aware event store.

Contract:
    event_id, event_type, instrument_id, account_id, market,
    detected_at, source_timestamp, severity, evidence_refs, status

Every event has a stable ``fingerprint`` (type + instrument + account +
rounded source timestamp). Receiving the same fingerprint again updates
the existing event's ``occurrences`` and ``last_seen`` — it never spawns
an unbounded stream of duplicates. A material state change (severity
escalation, condition cleared) IS a new event (different fingerprint
bucket via ``state_token``).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

SEVERITIES = ("INFO", "NOTICE", "WARNING", "CRITICAL")
EVENT_TYPES = frozenset({
    "price_move", "volume_spike", "ma_cross", "indicator_signal",
    "trend_change", "holding_pnl", "dividend_event", "nav_update",
    "nav_stale", "fund_distribution", "fund_fee", "fund_holdings_change",
    "allocation_drift", "concentration", "drawdown", "exposure_overlap",
    "data_stale", "data_incomplete", "opportunity", "risk_threshold",
    "system_fault", "report_ready",
})


def fingerprint(event_type: str, instrument_id: str, account_id: str,
                source_ts: float, state_token: str = "") -> str:
    raw = f"{event_type}|{instrument_id}|{account_id}|" \
          f"{int(source_ts // 3600)}|{state_token}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class MonitoringEventStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "monitoring"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "events.jsonl"
        self._fh = open(self._path, "a", encoding="utf-8")
        self._events: dict[str, dict[str, Any]] = {}   # fingerprint → row
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
                e = json.loads(line)
            except Exception:
                continue
            op = e.get("op")
            if op == "touch":
                cur = self._events.get(e.get("fingerprint"))
                if cur is not None:
                    cur["occurrences"] += 1
                    cur["last_seen"] = e.get("at", cur["last_seen"])
                continue
            if op == "resolve":
                for ev in self._events.values():
                    if ev["event_id"] == e.get("event_id"):
                        ev["status"] = "resolved"
                        ev["resolved_at"] = e.get("at")
                continue
            self._events[e["fingerprint"]] = e

    # ------------------------------------------------------------------
    def emit(self, event_type: str, *, instrument_id: str = "",
             account_id: str = "", market: str = "",
             source_timestamp: float | None = None,
             severity: str = "INFO", evidence_refs: list[str] | None = None,
             state_token: str = "", detail: dict[str, Any] | None = None
             ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            return {"ok": False, "error_code": "EVENT_TYPE_UNKNOWN"}
        if severity not in SEVERITIES:
            return {"ok": False, "error_code": "SEVERITY_UNKNOWN"}
        src_ts = float(source_timestamp or time.time())
        fp = fingerprint(event_type, instrument_id, account_id,
                         src_ts, state_token)
        now = time.time()
        existing = self._events.get(fp)
        if existing is not None:
            # dedup: bump occurrence count, never a new notification storm
            existing["occurrences"] += 1
            existing["last_seen"] = now
            self._fh.write(json.dumps(
                {"op": "touch", "fingerprint": fp, "at": now},
                ensure_ascii=False) + "\n")
            self._fh.flush()
            return {"ok": True, "event": dict(existing),
                    "deduplicated": True}
        ev = {
            "event_id": f"ev-{uuid.uuid4().hex[:12]}",
            "fingerprint": fp,
            "event_type": event_type,
            "instrument_id": str(instrument_id),
            "account_id": str(account_id),
            "market": str(market),
            "detected_at": now,
            "source_timestamp": src_ts,
            "severity": severity,
            "evidence_refs": list(evidence_refs or []),
            "status": "open",
            "occurrences": 1,
            "last_seen": now,
            "detail": dict(detail or {}),
        }
        self._events[fp] = ev
        self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._fh.flush()
        return {"ok": True, "event": dict(ev), "deduplicated": False}

    def resolve(self, event_id: str) -> dict[str, Any]:
        for e in self._events.values():
            if e["event_id"] == event_id:
                e["status"] = "resolved"
                e["resolved_at"] = time.time()
                self._fh.write(json.dumps(
                    {"op": "resolve", "event_id": event_id,
                     "at": e["resolved_at"]}, ensure_ascii=False) + "\n")
                self._fh.flush()
                return {"ok": True}
        return {"ok": False, "error_code": "EVENT_NOT_FOUND"}

    def list(self, *, severity: str | None = None,
             event_type: str | None = None,
             market: str | None = None,
             status: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        out = [e for e in self._events.values()
               if (severity is None or e["severity"] == severity)
               and (event_type is None or e["event_type"] == event_type)
               and (market is None or e["market"] == market)
               and (status is None or e["status"] == status)]
        return sorted(out, key=lambda e: e["detected_at"],
                      reverse=True)[:limit]
