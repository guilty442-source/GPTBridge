"""AutoTradePgMirror — mirrors autotrade orchestration state toward the
PostgreSQL canonical schema (migration 144, ``gptbridge_trading``)
through the governed business mirror channel.

Why not direct SQL: the tool never holds a business-schema write
credential. ``gptbridge_trading`` grants go to the business-owner role
(``gptbridge_index_executor``) and RLS pins writes to the owning
``module_id``; the least-privilege runtime login has no USAGE on the
schema by design. Like every other business record this tool produces
(fund NAV, paper executions, broker events), autotrade rows are
therefore enqueued into the service's bounded ``_mirror_outbox`` and
drained by ``_drain_mirror`` through the governed ``ChannelClient`` to
the ai-assistant business owner, which performs the actual INSERT with
its own module identity (RLS + ``simulated = true`` CHECKs still
enforce the boundary there).

Degrade contract:

* Sink bound (outbox sink callable) → mirror ops are enqueued; the
  local JSON/JSONL journal remains the runtime authority and PG is the
  durable copy once the channel drains.
* No sink → writes are *deferred* (counted, never raise); the trading
  path is unaffected and ``status()`` reports the dormant state
  honestly.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable

SCHEMA = "gptbridge_trading"

# Operation vocabulary follows the existing business-mirror convention
# (record_fund_nav / record_paper_execution / record_autotrade) — the
# ai-assistant business owner maps them onto the migration-144 tables.
OP_RUN = "record_autotrade_runtime"
OP_EVENT = "record_autotrade_runtime_event"
OP_SNAPSHOT = "record_autotrade_performance_snapshot"


class AutoTradePgMirror:
    """Outbox-backed PG mirror for autotrade runtime state."""

    def __init__(self, state_dir: Path,
                 sink: Callable[[dict[str, Any]], None] | None = None,
                 ) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sink = sink
        self._stats = {"enqueued": 0, "deferred": 0,
                       "last_error": "", "last_write_at": 0.0}

    # ------------------------------------------------------------------
    def bind(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Late-bind the service outbox sink (deque.append)."""
        self._sink = sink

    def _emit(self, op: dict[str, Any]) -> bool:
        if self._sink is None:
            self._stats["deferred"] += 1
            return False
        try:
            self._sink(op)
        except Exception as exc:
            self._stats["deferred"] += 1
            self._stats["last_error"] = f"{type(exc).__name__}"
            return False
        self._stats["enqueued"] += 1
        self._stats["last_write_at"] = time.time()
        return True

    # ------------------------------------------------------------------
    def mirror_run(self, run: dict[str, Any]) -> bool:
        """Upsert-equivalent of one strategy_runtime row (the business
        owner applies ON CONFLICT semantics against run_id)."""
        row = {
            "run_id": run["run_id"],
            "strategy_id": run["strategy_id"],
            "strategy_version": int(run.get("strategy_version") or 1),
            "strategy_type": str(run.get("strategy_type") or ""),
            "market": str(run.get("market") or ""),
            "instrument_scope": list(run.get("instrument_scope") or []),
            "parameters": dict(run.get("parameters") or {}),
            "execution_mode": str(run.get("execution_mode") or "SHADOW"),
            "ai_policy": str(run.get("ai_policy") or "DETERMINISTIC"),
            "account_id": run.get("account_id") or None,
            "auto_recover": bool(run.get("auto_recover")),
            "session_policy": dict(run.get("session_policy") or {}),
            "state": str(run.get("state") or "CREATED"),
            "state_reason": str(run.get("state_reason") or ""),
            "simulated": True,
            "created_at": float(run.get("created_at") or time.time()),
            "updated_at": float(run.get("updated_at") or time.time()),
        }
        return self._emit({"operation": OP_RUN, "schema": SCHEMA,
                           "table": "strategy_runtime", "run": row})

    def mirror_event(self, run_id: str, to_state: str, *,
                     from_state: str | None = None,
                     actor: str = "system", reason: str = "",
                     detail: dict[str, Any] | None = None) -> bool:
        """Append one strategy_runtime_event row (AI actors are blocked
        at source by the runtime SM; the table CHECK is the second
        gate)."""
        if actor.lower() in ("ai", "xingcheng", "model", "assistant") \
                and to_state in ("READY", "RUNNING"):
            return False  # never mirror an AI-authorized resume
        row = {
            "runtime_event_id": uuid.uuid4().hex,
            "run_id": run_id, "from_state": from_state,
            "to_state": to_state, "actor": actor,
            "reason": reason, "detail": dict(detail or {}),
        }
        return self._emit({"operation": OP_EVENT, "schema": SCHEMA,
                           "table": "strategy_runtime_event",
                           "event": row})

    def mirror_snapshot(self, run_id: str,
                        snapshot: dict[str, Any]) -> bool:
        """Append a performance snapshot row (all-simulated table)."""
        metrics = {k: v for k, v in snapshot.items()
                   if k not in {"run_id", "strategy_id",
                                "strategy_version", "account_id",
                                "equity", "cash_available",
                                "realized_pnl", "unrealized_pnl",
                                "orders", "filled", "win_rate", "at"}}
        row = {
            "snapshot_id": uuid.uuid4().hex,
            "run_id": run_id,
            "strategy_id": str(snapshot.get("strategy_id") or ""),
            "strategy_version": int(
                snapshot.get("strategy_version") or 1),
            "account_id": snapshot.get("account_id") or None,
            "equity": str(snapshot.get("equity") or "0"),
            "cash_available": str(
                snapshot.get("cash_available") or "0"),
            "realized_pnl": str(snapshot.get("realized_pnl") or "0"),
            "unrealized_pnl": str(
                snapshot.get("unrealized_pnl") or "0"),
            "orders": int(snapshot.get("orders") or 0),
            "filled": int(snapshot.get("filled") or 0),
            "win_rate": (str(snapshot["win_rate"])
                         if snapshot.get("win_rate") is not None
                         else None),
            "metrics": metrics,
            "simulated": True,
            "at": float(snapshot.get("at") or time.time()),
        }
        return self._emit({"operation": OP_SNAPSHOT, "schema": SCHEMA,
                           "table": "strategy_performance_snapshot",
                           "snapshot": row})

    # ------------------------------------------------------------------
    def flush(self, limit: int = 500) -> dict[str, Any]:
        """Kept for the maintenance surface: delivery itself is owned by
        ``_drain_mirror`` (bounded per service call, retrying on
        failure), so there is nothing to replay here."""
        return {"ok": True, "flushed": 0, "pending": 0,
                "note": "delivery via governed channel outbox"}

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "channel_bound": self._sink is not None,
            "target_schema": SCHEMA,
            **self._stats,
        }
