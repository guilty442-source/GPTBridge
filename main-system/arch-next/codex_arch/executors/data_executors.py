"""data executors — 資料完整性查核執行（stdlib：sqlite3/pathlib；唯讀）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..governance.delegation import ExecutorBinding

_DATA_DIRECTORY_DECLARATION: dict[str, str] = {
    "structured": "runtime/state",
    "semantic-index": "data/semantic-index",
    "version-history": ".git",
}


def _sql_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    target = str(payload.get("target") or "")
    if not target:
        connection = sqlite3.connect(":memory:")
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            return {"target": "in-memory", "integrity": str(result[0])}
        finally:
            connection.close()
    path = Path(target).resolve()
    if not path.is_file():
        return {"target": target, "integrity": "missing-target", "ok": False}
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except (sqlite3.DatabaseError, OSError) as error:
        return {"target": target, "integrity": error.__class__.__name__, "ok": False}
    return {"target": target, "integrity": str(result[0]), "ok": True}


def _semantic_index_health(payload: dict[str, Any]) -> dict[str, Any]:
    target = str(payload.get("target") or _DATA_DIRECTORY_DECLARATION["semantic-index"])
    root = Path(target).resolve()
    entries = sorted(p.name for p in root.glob("*")) if root.is_dir() else []
    return {
        "target": target,
        "present": root.is_dir(),
        "entries": entries,
        "manifest": "ok" if root.is_dir() else "missing",
    }


def _version_history_health(payload: dict[str, Any]) -> dict[str, Any]:
    target = str(payload.get("target") or _DATA_DIRECTORY_DECLARATION["version-history"])
    root = Path(target).resolve()
    head = root / "HEAD"
    return {
        "target": target,
        "present": root.is_dir(),
        "head_present": head.is_file(),
        "versioning": "git-local" if (root / "config").is_file() else "unknown",
    }


def _data_directory(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "directory": _DATA_DIRECTORY_DECLARATION,
        "owners": ["system-data-sub-sovereign"],
        "access": "deny-by-default-explicit-allow",
    }


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="sql-integrity",
            boundary="data-integrity-check",
            permission_intent="sql-integrity",
            owner_sovereign="data",
            target="system-data-sub-sovereign:data:sql-integrity",
            implementation=_sql_integrity,
        ),
        ExecutorBinding(
            executor_id="semantic-index-health",
            boundary="data-integrity-check",
            permission_intent="semantic-index-health",
            owner_sovereign="data",
            target="system-data-sub-sovereign:data:semantic-index-health",
            implementation=_semantic_index_health,
        ),
        ExecutorBinding(
            executor_id="version-history-health",
            boundary="data-integrity-check",
            permission_intent="version-history-health",
            owner_sovereign="data",
            target="system-data-sub-sovereign:data:version-history-health",
            implementation=_version_history_health,
        ),
        ExecutorBinding(
            executor_id="data-directory",
            boundary="data-directory",
            permission_intent="data-directory",
            owner_sovereign="data",
            target="system-data-sub-sovereign:data:data-directory",
            implementation=_data_directory,
        ),
    ]


__all__ = ["bindings"]