```yaml
project:
  name: GPTBridge
  baseline: Windows 11 Stable
  modules:
    backend: src-core (Python, Playwright)
    frontend: src-ui (React, Vite, Electron)
    config: root config files

environment:
  os: Windows 11
  shell: powershell
  shell_cmd: npm.cmd

execution_style:
  strategy: small_scope_execution
  max_files_per_task: 3
  concurrency: serial_only
  async_only: true
  api_mode: prohibited (browser_automation_only)
  quality:
    python: mandatory_type_hints
    typescript: strict_interfaces
    ui_language: traditional_chinese
    code_naming: english_only
  unsolicited_refactoring: prohibited

scope_rules:
  priority: task_scoped_files_only
  workspace_search: prohibited_unless_audit
  recursive_search: prohibited

forbidden_paths:
  directories:
    - node_modules/
    - .venv/
    - runtime/
    - backups/
    - dist*/
    - __pycache__/
    - logs/
    - coverage/
    - build/
    - out/
    - release/
    - .git/
    - LOG.old
  extensions: [.lock, .map, .exe, .dll, .pyd, .zip, .7z, .log, .tmp, .db, .sqlite]

browser_architecture:
  engine: Edge (shared_window_multi_tab)
  session_management: maintain_BrowserSessionManager_reuse
  persistence: runtime/profiles (read_only_access)

provider_rules:
  automation_mode: [chatgpt, gemini]
  preferred_provider:
    logic: chatgpt
    localization_zh: gemini
    long_text: gemini
  stability_note: "If Gemini auth fails, report 'Gemini 帳號需確認'"

stable_features:
  - Browser automation for ChatGPT and Gemini
  - Integrated React/Electron interface
  - Async/await based backend logic (Python/Playwright)

known_limitations:
  - Browser-only mode (API calls prohibited)
  - Serial task execution only (concurrency: serial_only)
  - Small scope execution (max 3 files)

startup_rules:
  command: npm.cmd run dev
  backend_port: 8765
  frontend_port: 5180
  conflict_check: true

fixed_urls:
  audit_chatgpt: https://chatgpt.com/c/6a0b05ab-e2ec-83a5-8140-ef41cff289a1
  audit_gemini: https://gemini.google.com/app/b209461647349329
  main_gemini: https://gemini.google.com/u/1/app/620770e7f10e50bf?pageId=none

cleanup_policy:
  protection:
    - core_configs
    - runtime/profiles
    - auth_data (cookies/storage)
  allowed_to_clean:
    - expired_logs
  backup_management: strict_size_control

storage_boundaries:
  sandbox_root: .GPTBridge_RuntimeSandbox
  backup_root: backups
  design_backup_root: backups/design-mode
  main_backup_root: backups/main-system
  rule: backups_and_sandbox_are_separate

audit_rules:
  full_scan: prohibited_without_audit_instruction
  instruction: "full audit"

reporting_rules:
  major_ops: OptimizationHistoryManager
  errors: log_hard_timeouts_and_exceptions

validation_rules:
  pre_execution: CommandPolicy_eval
  method: targeted_validation
  integrity: periodic_npm_run_build

governance_categorization:
  auto_decision: true
  base_path: resources/governance
  mapping:
    Dashboard: src-ui/dashboard/
    Toolbox: src-ui/toolbox/
    DeveloperMode: src-ui/developer_mode/
    Workspace: src-ui/developer_mode/workspace/
    DesignLab: src-ui/developer_mode/design_lab/
    RescueCenter: src-ui/developer_mode/rescue_center/
    SystemSettings: src-ui/developer_mode/system_settings/
    Deployment: src-ui/developer_mode/deployment/
    Provider_Browser_AI: src-core/providers/
    Tasks_Queue_Progress: src-core/tasks/
    IPC_Commands: src-core/ipc/
    Logging: src-core/logging/
    CoreSettings: src-core/settings/
    ToolSpecific: platform_tools/<tool_id>/
  fallback:
    path: common/
    flag: "classification_pending: true"
  rules:
    - never_ask_user_for_location
    - never_bloat_mega_files
    - auto_create_directories
    - merge_and_deduplicate_with_existing_rules
```
