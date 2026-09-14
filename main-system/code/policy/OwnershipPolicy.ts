/**
 * OwnershipPolicy.ts ?�Module ownership and assignment policy (A325/A356).
 *
 * Immutable data only. Defines how modules are assigned to sub-sovereigns.
 */

export interface OwnershipAssignment {
  modulePattern: string;      // Glob pattern for module IDs
  owner: string;              // Sub-sovereign ID
  primaryDomain: string;      // A324: one exclusive primary domain
  capabilities: string[];     // Declared capabilities (max 3 per A324)
  layer: string;
}

// Module ownership assignments per A325/A334
export const OWNERSHIP_ASSIGNMENTS: OwnershipAssignment[] = [
  // Decision Sovereign (A126/A127)
  {
    modulePattern: 'governance/sovereigns/decision_sovereign*',
    owner: 'decision-sovereign',
    primaryDomain: 'policy-architecture',
    capabilities: ['policy-adjudication', 'priority-delegation', 'change-acceptance'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/policy_architecture_sub_sovereign*',
    owner: 'decision-sovereign',
    primaryDomain: 'policy-architecture',
    capabilities: ['policy-architecture'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/health_maintenance_test_sub_sovereign*',
    owner: 'decision-sovereign',
    primaryDomain: 'health-maintenance',
    capabilities: ['health-monitoring', 'test-supervision'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/data_governance_sub_sovereign*',
    owner: 'decision-sovereign',
    primaryDomain: 'data-governance',
    capabilities: ['data-governance'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/change_acceptance_sub_sovereign*',
    owner: 'decision-sovereign',
    primaryDomain: 'change-acceptance',
    capabilities: ['change-acceptance'],
    layer: 'application-use-case',
  },

  // Permission Sovereign (A134)
  {
    modulePattern: 'governance/sovereigns/permission_sovereign*',
    owner: 'permission-sovereign',
    primaryDomain: 'permission-management',
    capabilities: ['permission-review', 'permission-authorize', 'directory-management'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/directory_sub_sovereign*',
    owner: 'permission-sovereign',
    primaryDomain: 'directory-management',
    capabilities: ['directory-registration'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/identity_group_sub_sovereign*',
    owner: 'permission-sovereign',
    primaryDomain: 'identity-group-management',
    capabilities: ['identity-group-management'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/language_review_sub_sovereign*',
    owner: 'permission-sovereign',
    primaryDomain: 'language-review',
    capabilities: ['language-review'],
    layer: 'application-use-case',
  },

  // System Runtime Sovereign (A128/A300)
  {
    modulePattern: 'governance/sovereigns/system_runtime_sovereign*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'runtime-management',
    capabilities: ['runtime-lifecycle', 'process-survival', 'health-monitoring'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/startup_sub_sovereign*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'startup-orchestration',
    capabilities: ['bootstrap', 'dependency-dag', 'readiness-handoff'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'src-core/startup_core/*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'startup-orchestration',
    capabilities: ['startup-phases', 'generation-management'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'src-core/boot_core*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'startup-orchestration',
    capabilities: ['backend-spawn', 'supervision'],
    layer: 'application-use-case',
  },

  // Synchronization Sovereign (A301/A322)
  {
    modulePattern: 'governance/sovereigns/synchronization_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'synchronization-policy',
    capabilities: ['sync-priority', 'dependency-order', 'conflict-disposition'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/channel_contract_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'channel-contract-sync',
    capabilities: ['channel-sync', 'contract-sync'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/dependency_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'dependency-governance',
    capabilities: ['inventory-version', 'license-security', 'dependency-sync'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'governance/sub-sovereigns/release_update_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'release-update-sync',
    capabilities: ['update-orchestration', 'hot-reload'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/update_manager*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'release-update-sync',
    capabilities: ['update-check', 'update-execute'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/hot_update*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'release-update-sync',
    capabilities: ['hot-reload', 'generation-prepare'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/system_automation_coordinator*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'synchronization-policy',
    capabilities: ['coordination', 'health-aggregation'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/resource_dependency_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'resource-allocation',
    capabilities: ['resource-monitoring', 'resource-release', 'tool-isolation'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/tool_isolation*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'resource-allocation',
    capabilities: ['tool-isolation', 'resource-quotas'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'governance/sub-sovereigns/cleanup_retention_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'cleanup-retention',
    capabilities: ['cleanup', 'retention'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/daily_global_cleaner*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'cleanup-retention',
    capabilities: ['global-cleaner'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'governance/sub-sovereigns/repair_backup_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'repair-backup',
    capabilities: ['backup', 'repair-sync'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/tasks/central_repair*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'auto-repair',
    capabilities: ['auto-repair', 'repair-coordination'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/learning_evidence_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'learning-evidence',
    capabilities: ['learning-evidence'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/tasks/repair_learning*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'learning-evidence',
    capabilities: ['learning-evidence'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'governance/sub-sovereigns/priority_capability_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'priority-capability',
    capabilities: ['priority-capability'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/runtime_state_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'runtime-state',
    capabilities: ['runtime-state-sync'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'governance/sub-sovereigns/automatic_log_sync_sub_sovereign*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'automatic-log',
    capabilities: ['automatic-log'],
    layer: 'infrastructure',
  },

  // Xingcheng Sovereign (A20/A138/A139)
  {
    modulePattern: 'governance/sovereigns/xingcheng_sovereign*',
    owner: 'xingcheng-sovereign',
    primaryDomain: 'global-review',
    capabilities: ['global-review', 'anomaly-classification', 'user-notification'],
    layer: 'domain',
  },
  {
    modulePattern: 'src-core/core_system/xingcheng_*',
    owner: 'xingcheng-sovereign',
    primaryDomain: 'global-review',
    capabilities: ['native-model', 'reasoning'],
    layer: 'domain',
  },

  // Information Layer (A65/A176/A186)
  {
    modulePattern: 'information-layer/*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'information-delivery',
    capabilities: ['session-management', 'message-delivery', 'contract-validation'],
    layer: 'channel-api',
  },
  {
    modulePattern: 'src-ui/*',
    owner: 'runtime-sovereign',
    primaryDomain: 'presentation',
    capabilities: ['ui-presentation', 'user-interaction'],
    layer: 'presentation',
  },
  {
    modulePattern: 'src-core/ipc/*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'information-delivery',
    capabilities: ['ipc-transport', 'command-routing'],
    layer: 'channel-api',
  },

  // Native Layer (A219/A220/A221)
  {
    modulePattern: 'native/core/*',
    owner: 'xingcheng-sovereign',
    primaryDomain: 'native-compute',
    capabilities: ['parser', 'vector', 'memory', 'token', 'transform', 'binding'],
    layer: 'native-core',
  },
  {
    modulePattern: 'native/bridge/*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'c-abi',
    capabilities: ['c-abi-thunk'],
    layer: 'c-abi',
  },
  {
    modulePattern: 'native/include/*',
    owner: 'synchronization-sovereign',
    primaryDomain: 'c-abi',
    capabilities: ['c-abi-contract'],
    layer: 'c-abi',
  },
  {
    modulePattern: 'src-core/core_system/native/_binding.cpp',
    owner: 'synchronization-sovereign',
    primaryDomain: 'pybind11-binding',
    capabilities: ['pybind11-binding'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'main-system/launcher/src/*',
    owner: 'runtime-sovereign',
    primaryDomain: 'csharp-adapter',
    capabilities: ['windows-launcher'],
    layer: 'csharp-adapter',
  },
];

export const SUB_SOVEREIGN_DOMAINS: Record<string, string> = {
  'policy-architecture-sub-sovereign': 'policy-architecture',
  'health-maintenance-test-sub-sovereign': 'health-maintenance',
  'data-governance-sub-sovereign': 'data-governance',
  'change-acceptance-sub-sovereign': 'change-acceptance',
  'directory-sub-sovereign': 'directory-management',
  'identity-group-sub-sovereign': 'identity-group-management',
  'language-review-sub-sovereign': 'language-review',
  'startup-sub-sovereign': 'startup-orchestration',
  'channel-contract-sync-sub-sovereign': 'channel-contract-sync',
  'dependency-sync-sub-sovereign': 'dependency-governance',
  'release-update-sync-sub-sovereign': 'release-update-sync',
  'resource-dependency-sync-sub-sovereign': 'resource-allocation',
  'cleanup-retention-sync-sub-sovereign': 'cleanup-retention',
  'repair-backup-sync-sub-sovereign': 'repair-backup',
  'learning-evidence-sync-sub-sovereign': 'learning-evidence',
  'priority-capability-sub-sovereign': 'priority-capability',
  'runtime-state-sync-sub-sovereign': 'runtime-state',
  'automatic-log-sync-sub-sovereign': 'automatic-log',
};

export const MAX_CAPABILITIES_PER_SUB_SOVEREIGN = 3;

export function getOwnerForModule(moduleId: string): { owner: string; domain: string; layer: string } | null {
  for (const assignment of OWNERSHIP_ASSIGNMENTS) {
    if (matchPattern(moduleId, assignment.modulePattern)) {
      return {
        owner: assignment.owner,
        domain: assignment.primaryDomain,
        layer: assignment.layer,
      };
    }
  }
  return null;
}

export function validateAssignment(assignment: OwnershipAssignment): string[] {
  const errors: string[] = [];

  if (assignment.capabilities.length > MAX_CAPABILITIES_PER_SUB_SOVEREIGN) {
    errors.push(`Owner ${assignment.owner} has ${assignment.capabilities.length} capabilities, max is ${MAX_CAPABILITIES_PER_SUB_SOVEREIGN} (A324)`);
  }

  if (!SUB_SOVEREIGN_DOMAINS[assignment.owner + '-sub-sovereign']) {
    // Check if it's a sovereign
    const sovereignDomains: Record<string, string> = {
      'decision-sovereign': 'policy-architecture',
      'permission-sovereign': 'permission-management',
      'system-runtime-sovereign': 'runtime-management',
      'synchronization-sovereign': 'synchronization-policy',
      'xingcheng-sovereign': 'global-review',
      'runtime-sovereign': 'presentation',
    };
    if (!sovereignDomains[assignment.owner]) {
      errors.push(`Unknown owner: ${assignment.owner}`);
    }
  }

  return errors;
}

function matchPattern(moduleId: string, pattern: string): boolean {
  const regex = pattern
    .replace(/\*/g, '.*')
    .replace(/\?/g, '.');
  return new RegExp(`^${regex}$`).test(moduleId);
}