"""AutoTradePgMirror — write-through mirror of autotrade orchestration
state into PostgreSQL ``gptbridge_trading`` (migration 144).

Design contract:

* The tool-local JSON/JSONL journal remains the runtime store; this
  mirror makes PostgreSQL the durable authoritative copy.
* No second connection pool — the shared governed
  ``shared_layer.database.connection.ConnectionManager`` is reused when
  present (peeked lazily, never created here; DSN purpose separation and
  bounded pool size stay governed by the shared layer).
* PG down → rows spool to ``pg_spool.jsonl`` and are flushed by
  maintenance/``flush()``; writes never raise into the trading path
  (degrade, not break), but every spool is auditable via ``status()``.
* Everything mirrored is simulated orchestration state — CHECK
  constraints on the target tables already pin ``simulated = true``.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

SCHEMA = "gptbridge_trading"


def _cm() -> Any:
    """Peek at the shared governed ConnectionManager, if one exists."""
    try:
        from shared_layer.database.connection import (
            peek_connection_manager)
        return peek_connection_manager()
    except Exception:
        return None


class AutoTradePgMirror:
    """Mirror autotrade runtime rows to PG; spool when unavailable."""

    SPOOL_MAX_LINES = 2000
    _SCHEMA_PROBE_TTL_S = 60.0

    def __init__(self, state_dir: Path,
                 conn_manager: Any = None) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._spool = self._dir / "pg_spool.jsonl"
        self._cm = conn_manager  # explicit injection wins (tests/host)
        self._schema_ok = False
        self._schema_probe_at = 0.0
        self._stats = {"written": 0, "spooled": 0, "flushed": 0,
                       "deferred": 0,
                       "last_error": "", "last_write_at": 0.0}

    # ------------------------------------------------------------------
    def _manager(self) -> Any:
        return self._cm if self._cm is not None else _cm()

    def _schema_present(self) -> bool:
        """Cached probe — the gptbridge_trading schema may land mid-run;
        re-probe after the TTL instead of assuming permanent absence."""
        if time.time() - self._schema_probe_at < self._SCHEMA_PROBE_TTL_S:
            return self._schema_ok
        mgr = self._manager()
        if mgr is None:
            self._schema_ok = False
            self._schema_probe_at = time.time()
            return False
        try:
            with mgr.connection() as conn:
                row = conn.execute(
                    "SELECT to_regclass('gptbridge_trading.strategy_runtime')"
                ).fetchone()
            self._schema_ok = bool(row and list(row)[0])
        except Exception:
            self._schema_ok = False
        self._schema_probe_at = time.time()
        return self._schema_ok

    def _execute(self, sql: str, args: tuple) -> None:
        mgr = self._manager()
        if mgr is None:
            raise RuntimeError("PG_UNAVAILABLE")
        with mgr.connection() as conn:
            # RLS write policies pin module_id to the session GUC; the
            # mirror writes on behalf of the ai-assistant business owner.
            conn.execute(
                "SET LOCAL app.current_module_id = 'ai-assistant'")
            conn.execute(sql, args)
            conn.commit()

    def _spool_append(self, kind: str, row: dict[str, Any]) -> None:
        with open(self._spool, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "row": row,
                                 "at": time.time()},
                                ensure_ascii=False, default=str) + "\n")
        # Bound the mirror backlog — the JSON journal stays authoritative,
        # so an over-long spool drops its oldest half rather than grow
        # without limit while PG is unreachable (A178).
        try:
            lines = self._spool.read_text(
                encoding="utf-8").splitlines()
            if len(lines) > self.SPOOL_MAX_LINES:
                self._spool.write_text(
                    "".join(l + "\n"
                            for l in lines[-self.SPOOL_MAX_LINES // 2:]),
                    encoding="utf-8")
        except OSError:
            pass

    def _write(self, sql: str, args: tuple, kind: str,
               row: dict[str, Any]) -> bool:
        if not self._schema_present():
            self._stats["deferred"] += 1
            return False
        try:
            self._execute(sql, args)
        except Exception as exc:
            self._stats["spooled"] += 1
            self._stats["last_error"] = f"{type(exc).__name__}"
            self._spool_append(kind, row)
            return False
        self._stats["written"] += 1
        self._stats["last_write_at"] = time.time()
        return True

    # ------------------------------------------------------------------
    def mirror_run(self, run: dict[str, Any]) -> bool:
        """Upsert one strategy_runtime row."""
        sql = (
            f"INSERT INTO {SCHEMA}.strategy_runtime "
            "(run_id, strategy_id, strategy_version, strategy_type, "
            "market, instrument_scope, parameters, execution_mode, "
            "ai_policy, account_id, auto_recover, session_policy, "
            "state, state_reason, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "to_timestamp(%s), to_timestamp(%s)) "
            "ON CONFLICT (run_id) DO UPDATE SET "
            "state=EXCLUDED.state, state_reason=EXCLUDED.state_reason, "
            "parameters=EXCLUDED.parameters, "
            "session_policy=EXCLUDED.session_policy, "
            "auto_recover=EXCLUDED.auto_recover, "
            "updated_at=EXCLUDED.updated_at")
        args = (
            run["run_id"], run["strategy_id"],
            int(run.get("strategy_version") or 1),
            str(run.get("strategy_type") or ""),
            str(run.get("market") or ""),
            json.dumps(run.get("instrument_scope") or []),
            json.dumps(run.get("parameters") or {}),
            str(run.get("execution_mode") or "SHADOW"),
            str(run.get("ai_policy") or "DETERMINISTIC"),
            run.get("account_id") or None,
            bool(run.get("auto_recover")),
            json.dumps(run.get("session_policy") or {}),
            str(run.get("state") or "CREATED"),
            str(run.get("state_reason") or ""),
            float(run.get("created_at") or time.time()),
            float(run.get("updated_at") or time.time()))
        return self._write(sql, args, "strategy_runtime", dict(run))

    def mirror_event(self, run_id: str, to_state: str, *,
                     from_state: str | None = None, actor: str = "system",
                     reason: str = "", detail: dict[str, Any] | None = None
                     ) -> bool:
        """Append one strategy_runtime_event row (AI actors blocked at
        source by the runtime SM; the table CHECK is the second gate)."""
        if actor.lower() in ("ai", "xingcheng", "model", "assistant") \
                and to_state in ("READY", "RUNNING"):
            return False  # never mirror an AI-authorized resume
        sql = (
            f"INSERT INTO {SCHEMA}.strategy_runtime_event "
            "(runtime_event_id, run_id, from_state, to_state, actor, "
            "reason, detail) VALUES (%s,%s,%s,%s,%s,%s,%s)")
        args = (uuid.uuid4().hex, run_id, from_state, to_state, actor,
                reason, json.dumps(detail or {}))
        return self._write(sql, args, "strategy_runtime_event",
                           {"run_id": run_id, "to_state": to_state})

    def mirror_snapshot(self, run_id: str,
                        snapshot: dict[str, Any]) -> bool:
        """Append a performance snapshot row (all-simulated table)."""
        sql = (
            f"INSERT INTO {SCHEMA}.strategy_performance_snapshot "
            "(snapshot_id, run_id, strategy_id, strategy_version, "
            "account_id, equity, cash_available, realized_pnl, "
            "unrealized_pnl, orders, filled, win_rate, metrics, at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "to_timestamp(%s))")
        metrics = {k: v for k, v in snapshot.items()
                   if k not in {"run_id", "strategy_id",
                                "strategy_version", "account_id",
                                "equity", "cash_available",
                                "realized_pnl", "unrealized_pnl",
                                "orders", "filled", "win_rate", "at"}}
        win_rate = snapshot.get("win_rate") or None
        args = (
            uuid.uuid4().hex, run_id,
            str(snapshot.get("strategy_id") or ""),
            int(snapshot.get("strategy_version") or 1),
            snapshot.get("account_id") or None,
            str(snapshot.get("equity") or "0"),
            str(snapshot.get("cash_available") or "0"),
            str(snapshot.get("realized_pnl") or "0"),
            str(snapshot.get("unrealized_pnl") or "0"),
            int(snapshot.get("orders") or 0),
            int(snapshot.get("filled") or 0),
            str(win_rate) if win_rate is not None else None,
            json.dumps(metrics, ensure_ascii=False, default=str),
            float(snapshot.get("at") or time.time()))
        return self._write(sql, args, "strategy_performance_snapshot",
                           {"run_id": run_id})

    # ------------------------------------------------------------------
    def flush(self, limit: int = 500) -> dict[str, Any]:
        """Retry spooled rows; returns counts. Idempotent — a row that
        fails again stays spooled (rewrite file minus flushed)."""
        if not self._spool.exists():
            return {"ok": True, "flushed": 0, "pending": 0}
        lines = self._spool.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        flushed = 0
        for line in lines[:limit]:
            try:
                rec = json.loads(line)
                kind, row = rec["kind"], rec["row"]
                if kind == "strategy_runtime":
                    ok = self.mirror_run(row)
                elif kind == "strategy_runtime_event":
                    ok = self.mirror_event(
                        row["run_id"], row["to_state"],
                        actor="recovery",
                        detail={"replayed_from_spool": True})
                elif kind == "strategy_performance_snapshot":
                    ok = self.mirror_snapshot(row["run_id"], row)
                else:
                    ok = False
            except Exception:
                ok = False
            if ok:
                flushed += 1
            else:
                kept.append(line)
        kept.extend(lines[limit:])
        self._spool.write_text(
            "".join(l + "\n" for l in kept), encoding="utf-8")
        self._stats["flushed"] += flushed
        return {"ok": True, "flushed": flushed, "pending": len(kept)}

    def status(self) -> dict[str, Any]:
        pending = 0
        if self._spool.exists():
            pending = sum(1 for l in self._spool.read_text(
                encoding="utf-8").splitlines() if l.strip())
        mgr = self._manager()
        return {
            "ok": True,
            "pg_available": mgr is not None,
            "schema_present": self._schema_present(),
            "pool": (mgr.stats() if mgr is not None
                     and hasattr(mgr, "stats") else None),
            "pending_spool": pending,
            **self._stats,
        }
