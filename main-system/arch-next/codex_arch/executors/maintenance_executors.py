"""maintenance executors — 健康彙整、故障判定、自動修復（呈現/計畫層）。

依 A33：健康彙整只「呈現」資料完整性健康，其查核執行歸資料執行器（data）。
本模組不執行任何資料完整性查核，僅彙整與呈現。
"""

from __future__ import annotations

from typing import Any

from ..governance.delegation import ExecutorBinding

_REPAIR_PLANS: dict[str, dict[str, Any]] = {
    "TOOL_START_FAILED": {"inspect": True, "rebuild": True, "action": "rebuild-tool-executable"},
    "BACKEND_CONNECTION_FAILED": {
        "inspect": True,
        "source_selfrepair": True,
        "action": "repair-main-system-source",
    },
    "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED": {
        "inspect": False,
        "source_selfrepair": True,
        "action": "repair-main-system-source",
    },
}


def _health_aggregate(payload: dict[str, Any]) -> dict[str, Any]:
    data_health = payload.get("data_integrity") or {}
    runtime = payload.get("runtime") or {}
    data_ok = bool(data_health.get("accepted"))
    return {
        "presented_by": "health-aggregate",
        "data_integrity_health": {"accepted": data_ok},
        "runtime": {"accepted": bool(runtime.get("accepted"))},
        "elevated": [data_health.get("health")] if data_ok else ["data-integrity-unavailable"],
    }


def _fault_plans(payload: dict[str, Any]) -> dict[str, Any]:
    failure_code = str(payload.get("failure_code") or "").strip().upper()
    plan = _REPAIR_PLANS.get(failure_code) or {
        "inspect": True,
        "rebuild": False,
        "action": "no-specific-plan",
    }
    return {"failure_code": failure_code, "plan": plan, "corroborated": True}


def _indentation_family(level: str, message: str) -> bool:
    if level in ("IndentationError", "TabError"):
        return True
    normalized = message.casefold()
    return any(
        signature in normalized
        for signature in (
            "unexpected indent",
            "unindent does not match any outer indentation level",
        )
    )


def _source_selfrepair(payload: dict[str, Any]) -> dict[str, Any]:
    source = str(payload.get("source") or "")
    if not source:
        return {"repaired": False, "reason": "no-source-provided", "executor": "source-selfrepair"}
    try:
        compile(source, "<repair>", "exec")
        return {"repaired": False, "reason": "already-compiles", "executor": "source-selfrepair"}
    except (IndentationError, TabError, SyntaxError) as error:
        level = error.__class__.__name__
        message = str(error)
    if not _indentation_family(level, message):
        return {
            "repaired": False,
            "reason": "not-indentation-family",
            "error": level,
            "executor": "source-selfrepair",
        }
    lines = source.splitlines()
    contentful = [i for i, line in enumerate(lines) if line.lstrip()]
    candidates: list[int] = []
    for i in range(len(contentful) - 1):
        current = contentful[i]
        if lines[current].lstrip() != lines[current]:
            continue
        prev = lines[contentful[i - 1]] if i > 0 else ""
        nxt = lines[contentful[i + 1]]
        if prev[:1] in (" ", "\t") and nxt[:1] in (" ", "\t"):
            candidates.append(current)
    patched = list(lines)
    applied: list[int] = []
    for index in reversed(candidates):
        indentation = ""
        for j in range(index - 1, -1, -1):
            if lines[j].lstrip() and lines[j][:1] in (" ", "\t"):
                indentation = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                break
        patched[index] = indentation + lines[index]
        applied.append(index)
    repaired_source = "\n".join(patched)
    try:
        compile(repaired_source, "<repair>", "exec")
    except (IndentationError, TabError, SyntaxError) as verify:
        return {
            "repaired": False,
            "reason": "verification-failed",
            "error": verify.__class__.__name__,
            "executor": "source-selfrepair",
        }
    return {
        "repaired": bool(applied),
        "repaired_lines": applied,
        "verification": "compile-ok",
        "executor": "source-selfrepair",
        "origin": "in-memory",
    }


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="health-aggregate",
            boundary="system-health-monitor",
            permission_intent="health-aggregate",
            owner_sovereign="maintenance",
            target="maintenance-sovereign:maintenance:health-aggregate",
            implementation=_health_aggregate,
        ),
        ExecutorBinding(
            executor_id="fault-plans",
            boundary="fault-determination",
            permission_intent="fault-plans",
            owner_sovereign="maintenance",
            target="maintenance-sovereign:maintenance:fault-plans",
            implementation=_fault_plans,
        ),
        ExecutorBinding(
            executor_id="source-selfrepair",
            boundary="automatic-repair",
            permission_intent="source-selfrepair",
            owner_sovereign="maintenance",
            target="maintenance-sovereign:maintenance:source-selfrepair",
            implementation=_source_selfrepair,
        ),
    ]


__all__ = ["bindings"]