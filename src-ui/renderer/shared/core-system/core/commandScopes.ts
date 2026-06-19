import type { SendCommand } from "../../types/ui";

type CommandScope = "design" | "rescue" | "developer" | "settings";

const ALLOWED_COMMANDS: Record<CommandScope, Set<string>> = {
  design: new Set([
    "discussion_query",
    "send_prompt",
    "design_backup",
    "design_ai_stop",
    "design_code_check",
    "design_delete_child_tool",
    "design_diff_view",
    "design_generate_child_tool",
    "design_modify_child_tool",
    "design_new_child_file",
    "design_new_project",
    "design_new_selected_file",
    "design_open_child_file",
    "design_open_project",
    "design_open_selected_file",
    "design_optimize_plan",
    "design_package_child_tool",
    "design_repair_chain",
    "design_release_summary",
    "design_rename_child_tool",
    "design_rollback_latest",
    "design_save_child_file",
    "design_test_child_tool",
  ]),
  rescue: new Set(["audit_run", "audit_stop"]),
  developer: new Set([
    "developer_auto_optimize",
    "developer_apply_sandbox",
    "developer_deploy_summary",
    "developer_phase1_integrity",
    "developer_phase2_static",
    "developer_phase3_startup",
    "developer_phase4_health",
    "developer_phase5_ai_review",
    "developer_phase6_build",
    "developer_prepare_sandbox",
    "toolbox_open_tool_code",
    "toolbox_save_tool_code",
  ]),
  settings: new Set([
    "change_provider_url",
    "focus_chatgpt",
    "focus_gemini",
    "load_config",
    "save_config",
    "settings_backup_records",
    "settings_delete_backup",
    "settings_export_error_logs",
    "settings_export_logs",
    "settings_health_refresh",
    "settings_maintain_sandbox",
    "settings_open_system_browser",
    "settings_reset_provider_profile",
  ]),
};

export const scopedSendCommand = (scope: CommandScope, sendCommand: SendCommand): SendCommand => {
  return (command, payload) => {
    if (!ALLOWED_COMMANDS[scope].has(command)) return;
    sendCommand(command, payload);
  };
};
