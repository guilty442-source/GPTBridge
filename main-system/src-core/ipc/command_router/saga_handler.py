"""Command Router — Saga Operations Handler (read-only diagnostics).

法典依據:
- A137: authenticated read-only evidence projection is the only system interface.
- Cross-engine workflow operations (migration 113) are the operation authority;
  this handler only projects their persisted state — it never mutates,
  claims, or reconciles.

Hardening controls:
- Both queries are read-only projections over the saga store; store access is
  bounded (``list_operations`` caps at 200 rows).
- Unknown operations fail closed (``OPERATION_NOT_FOUND``).
- A missing saga runtime degrades to ``SAGA_RUNTIME_UNAVAILABLE`` instead of
  an exception.
- Exception text is sanitized before reaching the UI (no internal paths or
  implementation details leak).
- Every query attempt/result/denial is recorded in a durable audit ledger.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_MAX_LIST_LIMIT = 200
_DEFAULT_LIST_LIMIT = 50

_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\s'\"]+|/[^\s'\"]+/")
_INTERNAL_DETAIL_PATTERN = re.compile(
    r"(?:Traceback|File |line \d+|in <module>|raise |except )",
    re.IGNORECASE,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_error_text(text: str) -> str:
    sanitized = _PATH_PATTERN.sub("<path>", text)
    sanitized = _INTERNAL_DETAIL_PATTERN.sub("", sanitized)
    return sanitized.strip()[:200] or "internal error"


class SagaOperationsHandler:
    """Handle ``app:get-saga-operations`` / ``app:get-saga-operation``."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._audit_ledger = (
            Path(__file__).resolve().parents[3]
            / "runtime"
            / "state"
            / "saga-query-audit.jsonl"
        )

    async def handle(
        self, command: str, payload: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        requester = str(payload.get("requester") or "ui").strip().lower()
        self._audit("saga-query-attempt", {"command": command, "requester": requester})
        store = self._store()
        if store is None:
            return self._deny(
                command, "SAGA_RUNTIME_UNAVAILABLE", "saga runtime not started"
            )
        if command == "app:get-saga-operations":
            return await self._list_operations(store, payload)
        return await self._operation_detail(store, payload)

    def _store(self) -> Any:
        runtime = getattr(self.app, "saga_runtime", None)
        services = getattr(runtime, "services", None)
        return getattr(services, "store", None)

    async def _list_operations(
        self, store: Any, payload: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        try:
            limit = int(payload.get("limit") or _DEFAULT_LIST_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIST_LIMIT
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        try:
            operations = await asyncio.to_thread(store.list_operations, limit)
        except Exception as error:
            return self._deny(
                "app:get-saga-operations",
                "SAGA_STORE_ERROR",
                _sanitize_error_text(f"{type(error).__name__}: {error}"),
            )
        self._audit("saga-query-result", {
            "command": "app:get-saga-operations",
            "count": len(operations),
        })
        return "app:get-saga-operations_result", {
            "ok": True,
            "operations": operations,
        }

    async def _operation_detail(
        self, store: Any, payload: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        operation_id = str(payload.get("operation_id") or "").strip()
        if not operation_id:
            return self._deny(
                "app:get-saga-operation",
                "MISSING_OPERATION_ID",
                "operation_id is required",
            )
        try:
            operation = await asyncio.to_thread(store.load_operation, operation_id)
            if operation is None:
                return self._deny(
                    "app:get-saga-operation",
                    "OPERATION_NOT_FOUND",
                    f"operation '{operation_id}' not found",
                )
            step_rows = await asyncio.to_thread(store.list_step_rows, operation_id)
            from shared_layer.workflow.visualization import (
                create_saga_visualizer,
                to_panel_payload,
            )
            data = create_saga_visualizer().visualize_stored_operation(
                operation, step_rows
            )
        except Exception as error:
            return self._deny(
                "app:get-saga-operation",
                "SAGA_STORE_ERROR",
                _sanitize_error_text(f"{type(error).__name__}: {error}"),
            )
        self._audit("saga-query-result", {
            "command": "app:get-saga-operation",
            "operation_id": operation_id,
        })
        return "app:get-saga-operation_result", {
            "ok": True,
            "operation": to_panel_payload(data),
        }

    def _deny(
        self, command: str, code: str, message: str
    ) -> tuple[str, dict[str, Any]]:
        self._audit("saga-query-denial", {
            "command": command,
            "error_code": code,
            "message": message,
        })
        return f"{command}_result", {
            "ok": False,
            "error_code": code,
            "message": message,
        }

    def _audit(self, event: str, extra: dict[str, Any]) -> None:
        record = {"event": event, "timestamp": _iso_now(), **extra}
        try:
            self._audit_ledger.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            with self._audit_ledger.open("a", encoding="utf-8") as handle:
                handle.write(line + os.linesep)
                handle.flush()
        except OSError:
            pass  # audit persistence is best-effort


__all__ = ["SagaOperationsHandler"]
