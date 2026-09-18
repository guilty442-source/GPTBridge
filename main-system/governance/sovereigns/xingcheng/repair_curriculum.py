"""Xingcheng repair curriculum — codified repair doctrine (A485).

The curriculum is 星澄's declared repair knowledge for the fault classes
the system actually emits.  Entries are applied by the learning
sub-sovereign when ``learn.auto-start`` arms it — stored as taught
recipes (``source="taught"``) so the learning module carries baseline
knowledge instead of starting cold.

Bounds (same contract as learned recipes):

- ``remedy`` uses only the runtime-safe token vocabulary
  (``TEACHABLE_REMEDY_TOKENS``); no taught recipe may arm a source
  mutation as an automatic plan.
- ``verification`` declares what must hold after execution — doctrine,
  not outcome-earned proof.
- ``failure_signatures`` match the error classes / failure codes the
  repair chain emits (error_signatures, PACKAGE_REBUILD_FAILURES).
"""

from __future__ import annotations

from typing import Any, Final

REPAIR_CURRICULUM: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "工具運行時崩潰→檢查後受管重建",
        "failure_signatures": (
            "TOOL_RUNTIME_CRASH",
            "TOOL_START_FAILED",
            "PROCESS_START_FAILED",
        ),
        "remedy": "inspect-owned-databases,rebuild-tool-executable",
        "verification": (
            "owned databases inspected; rebuilt executable starts and the "
            "tool returns to ready; stderr diagnosis preserved as quarantine "
            "evidence"
        ),
    },
    {
        "name": "啟動崩潰→檢查後受管重建",
        "failure_signatures": ("STARTUP_CRASH",),
        "remedy": "inspect-owned-databases,rebuild-tool-executable",
        "verification": (
            "health probe returns ready with a matching workspace instance "
            "id; import-family faults escalate to governed source repair "
            "instead of rebuild"
        ),
    },
    {
        "name": "依賴未就緒→檢查後受管重建",
        "failure_signatures": (
            "MODEL_RUNTIME_NOT_READY",
            "SOURCE_RUNTIME_NOT_READY",
            "SOURCE_UI_UNAVAILABLE",
            "SOURCE_RUNTIME_EXITED",
            "QDRANT_UNAVAILABLE",
            "EMBEDDING_UNAVAILABLE",
        ),
        "remedy": "inspect-owned-databases,rebuild-tool-executable",
        "verification": (
            "dependency health check reports available after governed "
            "rebuild; unrecoverable dependencies escalate for user review"
        ),
    },
    {
        "name": "套件與執行檔故障→受管重建",
        "failure_signatures": (
            "EXECUTABLE_MISSING",
            "STALE_TOOL_PACKAGE",
            "PACKAGE_UNVERIFIED",
            "INCOMPATIBLE_TOOL_RUNTIME",
            "TOOL_VERSION_MISMATCH",
        ),
        "remedy": "inspect-owned-databases,rebuild-tool-executable",
        "verification": (
            "rebuilt executable launches and reports a version matching the "
            "manifest"
        ),
    },
    {
        "name": "連線故障→檢查後受管恢復",
        "failure_signatures": (
            "BACKEND_CONNECTION_FAILED",
            "FRONTEND_BACKEND_DISCONNECTED",
            "COMMAND_EXECUTION_FAILED",
            "STREAM_CHANNEL_FAILED",
        ),
        "remedy": "inspect-owned-databases,rebuild-tool-executable",
        "verification": (
            "backend /health returns 200 and the frontend WebSocket "
            "reconnects within the probe interval"
        ),
    },
    {
        "name": "連線暫態→觀察不修",
        "failure_signatures": (
            "CONNECTION_DEGRADED",
            "CONNECTION_STARTING",
        ),
        "remedy": "no-action-required",
        "verification": (
            "transient connection states self-recover; sustained "
            "degradation escalates to the connection-fault recipe instead "
            "of burning retries"
        ),
    },
    {
        "name": "主系統崩潰→僅檢查，人工修復",
        "failure_signatures": ("MAIN_SYSTEM_CRASH_REPAIR",),
        "remedy": "inspect-owned-databases",
        "automatic": False,
        "verification": (
            "inspection evidence recorded for the fault surface; main-system "
            "source repair stays a governed manual path — never automatic"
        ),
    },
    {
        "name": "未知故障→僅檢查留證",
        "failure_signatures": ("UNKNOWN",),
        "remedy": "inspect-owned-databases",
        "automatic": False,
        "verification": (
            "inspection evidence recorded; unknown faults never receive an "
            "automatic remedy until outcomes earn one"
        ),
    },
)


__all__ = ["REPAIR_CURRICULUM"]
