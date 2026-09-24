"""Command Router — Constants and Routing Tables."""

from __future__ import annotations

from typing import Any, Dict

TOOL_LIFECYCLE_HANDLERS: Dict[str, str] = {
    "toolbox_list_tools": "list_tools",
    "toolbox_start_tool": "start_tool",
    "toolbox_stop_tool": "stop_tool",
    "toolbox_force_close_tool": "force_close_tool",
    "toolbox_request_tool_execution": "request_tool_execution",
    "toolbox_run_tool": "run_tool",
    "toolbox_cancel_tool_execution": "cancel_tool_execution",
    "toolbox_cancel_tool_run": "cancel_tool_execution",
}

MAIN_COMMANDS = {
    "toolbox_list_tools",
    "toolbox_start_tool",
    "toolbox_stop_tool",
    "toolbox_force_close_tool",
    "toolbox_request_tool_execution",
    "toolbox_run_tool",
    "toolbox_cancel_tool_execution",
    "toolbox_cancel_tool_run",
    "app:get-runtime-status",
    "app:get-governance-rules",
    "app:run-main-system-self-maintenance",
    "app:get-third-party-status",
    "app:probe-third-party-versions",
    "app:check-third-party-updates",
    "app:update-third-party-tool",
    "app:auto-update-third-party-tools",
    "app:get-repair-status",
    "app:hot-reload-backend",
    "app:get-fault-analysis",
    "app:get-saga-operations",
    "app:get-saga-operation",
    "app:get-pending-actions",
    "app:get-automation-switches",
    "app:set-automation-switch",
    "app:get-resource-mode",
    "app:set-resource-mode",
    "xingcheng-set-repair-release",
    "xingcheng-set-update-release",
    "xingcheng-set-native-model-enabled",
    "xingcheng-confirm-automatic-repair",
    "xingcheng-confirm-automatic-update",
    "xingcheng-deny-pending-action",
    "xingcheng-revoke-automatic-repair-confirmation",
    "xingcheng-revoke-automatic-update-confirmation",
    "sync-execute-approved-automatic-repair",
    "sync-execute-approved-automatic-update",
}


def _pending_action_cardinality(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """A366 CARDINALITY snapshot for the assistant panel."""
    actionable = [
        action
        for action in actions
        if action.get("status") == "awaiting-confirmation"
    ]
    repairs = [action for action in actionable if action.get("kind") == "repair"]
    updates = [action for action in actionable if action.get("kind") == "update"]
    if len(repairs) >= 2:
        mode = "MULTI_FAULT"
    elif len(repairs) == 1:
        mode = "SINGLE_FAULT"
    else:
        mode = "NO_FAULT"
    return {
        "mode": mode,
        "total_actionable": len(actionable),
        "fault_count": len(repairs),
        "update_count": len(updates),
        "unresolved": len(actionable),
    }
