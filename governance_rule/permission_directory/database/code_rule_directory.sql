-- Code Rule Directory — SQL authority (migrated from Python tuple per user: Python 目錄改用SQL)
-- Authority: permission-sovereign via codex; this table is the derived SQL store for CodeRuleDirectorySnapshot.
-- Deleting this file's table is safe; regenerate via the Python snapshot fallback.

CREATE TABLE IF NOT EXISTS code_rule_directory (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    authority_version INTEGER NOT NULL,
    managing_authority TEXT NOT NULL,
    governing_source TEXT NOT NULL,
    independent_authority INTEGER NOT NULL,
    runtime_write_allowed INTEGER NOT NULL,
    canonical_project_root TEXT NOT NULL,
    main_system_root TEXT NOT NULL,
    shared_layer_root TEXT NOT NULL,
    governance_root TEXT NOT NULL,
    permission_directory_root TEXT NOT NULL,
    independent_tool_root_template TEXT NOT NULL,
    governance_execution_root TEXT NOT NULL,
    permission_execution_root TEXT NOT NULL,
    path_resolution TEXT NOT NULL,
    source_language TEXT NOT NULL,
    localization_source TEXT NOT NULL,
    initial_code_version TEXT NOT NULL,
    implicit_version_change INTEGER NOT NULL,
    identifier_label_schema TEXT NOT NULL,
    approved_tool_ids TEXT NOT NULL,
    approved_actor_names TEXT NOT NULL,
    approved_capability_names TEXT NOT NULL,
    approved_action_names TEXT NOT NULL,
    approved_target_names TEXT NOT NULL,
    approved_data_scope_names TEXT NOT NULL,
    required_locale_keys TEXT NOT NULL,
    category_labels INTEGER NOT NULL,
    requirements TEXT NOT NULL
);

-- Seed the single row from the previous Python tuple (CODE_RULE_DIRECTORY).
-- JSON arrays are stored as TEXT (JSON) for approved_* and requirements.
INSERT OR IGNORE INTO code_rule_directory (
    id, authority_version, managing_authority, governing_source, independent_authority, runtime_write_allowed,
    canonical_project_root, main_system_root, shared_layer_root, governance_root, permission_directory_root,
    independent_tool_root_template, governance_execution_root, permission_execution_root,
    path_resolution, source_language, localization_source, initial_code_version, implicit_version_change,
    identifier_label_schema, approved_tool_ids, approved_actor_names, approved_capability_names,
    approved_action_names, approved_target_names, approved_data_scope_names, required_locale_keys,
    category_labels, requirements
) VALUES (
    1, 1, 'codex-via-enforcement-policy', 'governance_rule/codex/__init__.py', 0, 0,
    'E:/GPTBridge', 'main-system', 'shared-layer', 'governance_rule', 'governance_rule/permission_directory',
    '{tool_id}', 'governance_rule/execution', 'governance_rule/permission_directory/execution',
    'canonical-authority-path-module-only', 'programming-language-only', 'external-language-plugin-only', '1.00000', 0,
    'gptbridge-identifier-label-v1',
    '["ai-assistant","ai-collaboration","file-sorter","global-cleaner","governance_rule","investment-mobile","local-model","model-dialogue","xingcheng","shared-layer","system-rescue","vaultly"]',
    '["governance/main-system","governance/tool/ai-assistant","governance/tool/ai-collaboration","governance/tool/file-sorter","governance/tool/global-cleaner","governance/tool/governance_rule","governance/tool/investment-mobile","governance/tool/local-model","governance/tool/model-dialogue","governance/tool/xingcheng","governance/tool/xingcheng-assistant","governance/tool/shared-layer","governance/tool/system-rescue","governance/tool/vaultly"]',
    '["ai-channel-top-level","ai-connections","authenticated-ipc","external-ai","frontend-backend-connection-stability","global-cleanup","managed-backup","governance","codex-read-execute","hot-update","independent-tool-business-logic","independent-tool-business-storage","independent-tool-discovery","independent-tool-start-and-stop","independent-tool-user-settings","investment-analysis","investment-manager","investment-market-search","xingcheng","local-model-platform-execution","permission-directory-read-execute","star-investment-manager-database-read","star-decision","star-global-data-read","star-internal-data-read-write","star-project-database-operations","star-project-programming","system-channel-request-process","system-channel-request-submit","ai-channel-request-process","ai-channel-request-submit","system-health-check","central-automatic-repair","upgrade-optimization","xingcheng-governance-source-read","xingcheng-fault-analysis-read"]',
    '["append","approve","authorize","cancel-request","claim","check","connect","create-backup","delete","deny","delete-excess","diagnose","disconnect","discover","enforce","execute","extract-backup","health-check","read","request-read","request-reassessment","consume-response","read-source","receive-external-response","repair","request","request-external-collaboration","respond","rollback","stabilize","start","stop","update","verify","write"]',
    '["authenticated-ipc","authority-files","ai-channel:xingcheng","capability-request","central-index","frontend-backend-connection","global-cleanup-candidates","global-source","global-system-health","codex-snapshot","governed-operation","governed-decision","independent-tool-manifests","permission-directory-module","directory-authority-snapshot","non-governance-project-source","non-governance-project-database","owner-resource-resolve:{tool_id}","shared-layer-system-request:{tool_id}","shared-layer-ai-request:{tool_id}","managed-backup-root","local-model-runtime","automatic-repair-database:{tool_id}","backup-owner-source","backup-extract-staging","star-internal-data","tool-business-storage:{tool_id}","tool-business-storage:ai-assistant","tool-code:{tool_id}","tool-process:{tool_id}","tool-runtime:governance_rule","tool-runtime:{tool_id}","tool-settings:{tool_id}","validated-global-garbage"]',
    '["connection-metadata","global-read-only","main-runtime-state","xingcheng-internal-data","none","owner-scoped-backup","backup-extract","ai-assistant-investment-database","shared-layer-system-request","shared-layer-ai-request","non-governance-project-source","non-governance-project-database","opaque-locator-id-only","opaque-resource-index","tool-business:{tool_id}","tool-code","tool-settings:{tool_id}","tool-stability:{tool_id}","automatic-repair-knowledge","automatic-repair-runs","crash-diagnosis-records","repair-requests","system-health-snapshot","tool-crash-quarantine","audit-records","runtime-logs"]',
    '["tool.name","tool.window_title","tool.description","errors.permission_denied"]',
    0,
    '["codex-v1.32010-is-sole-rule-source","all-source-code-within-canonical-project-root","main-system-code-within-main-system-root-only","independent-tool-code-within-own-direct-root-only","no-nested-shared-independent-tool-parent","no-cross-tool-source-or-storage-access-outside-governed-shared-layer","shared-layer-is-an-independent-governance-jurisdiction-module","shared-layer-persists-central-index-relations-status-audit-and-transit-requests","postgresql-is-central-structured-index-source-of-truth","module-original-data-remains-in-owner-private-storage","central-index-physical-path-storage-and-disclosure-prohibited","opaque-locator-resolved-only-after-governance-by-owning-module-or-shared-layer","cross-module-data-access-default-deny-at-governance-shared-layer-and-postgresql","qdrant-semantic-index-is-vector-retrieval-only-with-governed-module-label-filters","star-global-read-highest-decision-no-external-execution","star-internal-data-read-write-permission-files-read-only","shared-layer-source-write-and-executable-content-prohibited","unchanneled-main-to-tool-and-tool-to-tool-instructions-denied","layered-and-modular-single-responsibility-code","database-backed-owner-isolated-persistent-state","permission-and-capability-boundaries-separated","governance-activation-before-permission-distribution","governance-default-direct-load-before-main-system","governance-rule-is-only-independent-tool-packaging-exception","governance-rule-read-only-direct-execution-without-executable-package","governance-encapsulation-stop-disable-and-unload-prohibited","governance-authorized-stability-repair-only","automatic-repair-backup-assistance-via-governed-channel-only","automatic-repair-backup-request-read-return-apply-verify-each-authorized","automatic-repair-has-no-direct-backup-storage-access","explicit-higher-version-required-for-code-update","all-identifiers-and-label-keys-match-governance-schema","approved-identifiers-listed-in-code-rule-directory-only","display-labels-loaded-from-owner-locale-plugin-only","category-labels-and-identifier-aliases-prohibited"]'
);
