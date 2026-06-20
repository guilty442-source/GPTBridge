from __future__ import annotations


APP_CODE_COMMANDS = {
    "app:save-code": "save_code",
    "app:delete-code": "delete_code",
    "app:diagnose-code": "diagnose_code",
    "app:move-code": "move_code",
    "app:update-config": "update_config",
    "app:agent-intervention": "agent_intervention",
    "app:agent-instruct": "agent_instruct",
    "app:agent-execute-tool": "agent_execute_tool",
    "app:run-unit-tests": "run_unit_tests",
}

GOVERNANCE_COMMANDS = {
    "app:get-governance-rules": "get_governance_rules",
    "app:set-governance-rules": "set_governance_rules",
    "app:add-governance-rule": "add_governance_rule",
    "app:delete-governance-rule": "delete_governance_rule",
    "app:update-governance-rule": "update_governance_rule",
}

TOOLBOX_COMMANDS = {
    "toolbox_add_tool": "add_tool",
    "toolbox_list_tools": "list_tools",
    "toolbox_start_tool": "start_tool",
    "toolbox_stop_tool": "stop_tool",
    "toolbox_run_tool": "run_tool",
    "toolbox_cancel_tool_run": "cancel_tool_run",
    "toolbox_open_tool_code": "open_tool_code",
    "toolbox_save_tool_code": "save_tool_code",
}

SETTINGS_COMMANDS = {
    "load_config": "load_config",
    "save_config": "save_config",
    "settings_health_refresh": "settings_health_refresh",
    "settings_mark_updates_applied": "settings_mark_updates_applied",
    "settings_maintain_sandbox": "settings_maintain_sandbox",
    "settings_backup_records": "settings_backup_records",
    "settings_delete_backup": "settings_delete_backup",
    "settings_export_logs": "settings_export_logs",
    "settings_export_error_logs": "settings_export_error_logs",
    "settings_reset_provider_profile": "settings_reset_provider_profile",
    "settings_open_system_browser": "settings_open_system_browser",
    "settings_factory_reset": "settings_factory_reset",
}

COMMAND_MAP = {
    **APP_CODE_COMMANDS,
    **GOVERNANCE_COMMANDS,
    **TOOLBOX_COMMANDS,
    **SETTINGS_COMMANDS,
}


def command_event(command: str) -> str:
    return f"{command}_result"


def is_known_command(command: str) -> bool:
    return command in COMMAND_MAP
