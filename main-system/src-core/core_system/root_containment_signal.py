"""Root containment signal functions — A201/E175, A202/E176.

Signal production extracted from root_containment for source-size
compliance (A185/E160).  Signals are read-only; they never mutate state.
"""

from __future__ import annotations

from typing import Any

from core_system.root_containment_types import (
    CodeRootCheck,
    EnvironmentRootCheck,
)


def code_root_violation_signal(
    check: CodeRootCheck,
    *,
    operation: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Produce a fail-closed signal for a code root violation (A201: FAILURE).

    Per A201: ``FAILURE:any-unresolved/ambiguous/nonexistent/parent/reparse-
    loop/case-alias/device-path/network-path/outside-root=>fail-closed+no-
    create/write/copy/move/load/import/execute+report-to-user``.
    """
    return {
        "signal_type": "code-root-violation",
        "authority": "signal-only",
        "basis": "A201/E175",
        "ok": check.ok,
        "requested_path": check.requested_path,
        "resolved_path": check.resolved_path,
        "root": check.root,
        "violation": check.violation,
        "operation": operation,
        "actor": actor,
        "action": "fail-closed+no-create/write/copy/move/load/import/execute+report-to-user",
        "quarantine": True,
    }


def environment_root_violation_signal(
    check: EnvironmentRootCheck,
    *,
    dependency_id: str = "",
) -> dict[str, Any]:
    """Produce a fail-closed signal for an environment root violation (A202: FAILURE).

    Per A202: ``FAILURE:outside/missing/ambiguous/unverified dependency-or-
    exception=>deny-load/execute+affected-capability-unavailable+typed-report``.
    """
    return {
        "signal_type": "environment-root-violation",
        "authority": "signal-only",
        "basis": "A202/E176",
        "ok": check.ok,
        "requested_path": check.requested_path,
        "resolved_path": check.resolved_path,
        "root": check.root,
        "violation": check.violation,
        "dependency_id": dependency_id,
        "action": "deny-load/execute+affected-capability-unavailable+typed-report",
        "windows11_exception": check.is_windows11_native_exception,
        "ollama_exception": check.is_ollama_c_drive_exception,
    }
