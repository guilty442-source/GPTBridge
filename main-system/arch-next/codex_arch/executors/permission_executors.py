"""permission executors — 權限存簿執行器（發放/終止之稽核紀錄）。"""

from __future__ import annotations

from typing import Any

from ..governance.delegation import ExecutorBinding

_ledger: list[dict[str, str]] = []


def _ledger_append(payload: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "operation": str(payload.get("operation") or "record"),
        "permission_id": str(payload.get("permission_id") or ""),
        "target": str(payload.get("target") or ""),
    }
    _ledger.append(entry)
    return {"sequence": len(_ledger), "entry": entry, "executor": "permission-ledger-append"}


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="permission-ledger-append",
            boundary="permission-record-keeping",
            permission_intent="permission-ledger-append",
            owner_sovereign="permission",
            target="permission-sovereign:permission:ledger-append",
            implementation=_ledger_append,
        ),
    ]


__all__ = ["bindings"]