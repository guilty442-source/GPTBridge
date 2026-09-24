/**
 * OwnershipPolicy.ts — module ownership and assignment policy (A325/A356).
 *
 * Immutable data only. Defines how modules are assigned to the five peer
 * cores (A604: the sub-sovereign layer is eliminated; modules are owned
 * directly by a core or by the A485 星澄-owned learning module).
 */

export interface OwnershipAssignment {
  modulePattern: string;      // Glob pattern for module IDs
  owner: string;              // Current core sovereign ID
  primaryDomain: string;      // A324: one exclusive primary domain
  capabilities: string[];     // Declared capabilities (max 3 per A324)
  layer: string;
}

// Module ownership assignments per A325/A604
export const OWNERSHIP_ASSIGNMENTS: OwnershipAssignment[] = [
  // Decision Sovereign (A126/A127)
  {
    modulePattern: 'governance/sovereigns/decision_sovereign*',
    owner: 'decision-sovereign',
    primaryDomain: 'policy-architecture',
    capabilities: ['policy-adjudication', 'priority-delegation', 'change-acceptance'],
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

  // System Runtime Sovereign (A128/A300)
  {
    modulePattern: 'governance/sovereigns/system_runtime_sovereign*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'runtime-management',
    capabilities: ['runtime-lifecycle', 'process-survival', 'health-monitoring'],
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

  // Automation Sovereign (A301/A322/A486)
  {
    modulePattern: 'governance/sovereigns/automation_sovereign*',
    owner: 'automation-sovereign',
    primaryDomain: 'synchronization-policy',
    capabilities: ['sync-priority', 'dependency-order', 'conflict-disposition'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'src-core/core_system/update_manager*',
    owner: 'automation-sovereign',
    primaryDomain: 'release-update-sync',
    capabilities: ['update-check', 'update-execute'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/hot_update*',
    owner: 'automation-sovereign',
    primaryDomain: 'release-update-sync',
    capabilities: ['hot-reload', 'generation-prepare'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/system_automation_coordinator*',
    owner: 'automation-sovereign',
    primaryDomain: 'synchronization-policy',
    capabilities: ['coordination', 'health-aggregation'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'src-core/core_system/tool_isolation*',
    owner: 'automation-sovereign',
    primaryDomain: 'resource-allocation',
    capabilities: ['tool-isolation', 'resource-quotas'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/core_system/daily_global_cleaner*',
    owner: 'automation-sovereign',
    primaryDomain: 'cleanup-retention',
    capabilities: ['automatic-cleanup'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'src-core/tasks/central_repair*',
    owner: 'automation-sovereign',
    primaryDomain: 'auto-repair',
    capabilities: ['auto-repair', 'repair-coordination'],
    layer: 'application-use-case',
  },
  {
    modulePattern: 'src-core/tasks/repair_learning*',
    owner: 'automation-sovereign',
    primaryDomain: 'learning-evidence',
    capabilities: ['learning-evidence'],
    layer: 'infrastructure',
  },

  // Xingcheng assistant core (A20/A138/A139) — codex identity 星澄
  {
    modulePattern: 'governance/sovereigns/xingcheng_sovereign*',
    owner: '星澄',
    primaryDomain: 'global-review',
    capabilities: ['global-review', 'anomaly-classification', 'user-notification'],
    layer: 'domain',
  },
  {
    modulePattern: 'src-core/core_system/xingcheng_*',
    owner: '星澄',
    primaryDomain: 'global-review',
    capabilities: ['native-model', 'reasoning'],
    layer: 'domain',
  },
  // A485/A604: learning is a 星澄-internal capability (no module layer);
  // the retired sub-sovereign codex identity survives as lineage only.
  {
    modulePattern: 'governance/sovereigns/xingcheng/learning_engine*',
    owner: '星澄',
    primaryDomain: 'learning-evidence',
    capabilities: ['learning-evidence'],
    layer: 'application-use-case',
  },

  // Information Layer (A65/A176/A186)
  {
    modulePattern: 'information-layer/*',
    owner: 'automation-sovereign',
    primaryDomain: 'information-delivery',
    capabilities: ['session-management', 'message-delivery', 'contract-validation'],
    layer: 'channel-api',
  },
  {
    modulePattern: 'src-ui/*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'presentation',
    capabilities: ['ui-presentation', 'user-interaction'],
    layer: 'presentation',
  },
  {
    modulePattern: 'src-core/ipc/*',
    owner: 'automation-sovereign',
    primaryDomain: 'information-delivery',
    capabilities: ['ipc-transport', 'command-routing'],
    layer: 'channel-api',
  },

  // Native Layer (A219/A220/A221)
  {
    modulePattern: 'native/core/*',
    owner: '星澄',
    primaryDomain: 'native-compute',
    capabilities: ['parser', 'vector', 'memory', 'token', 'transform', 'binding'],
    layer: 'native-core',
  },
  {
    modulePattern: 'native/bridge/*',
    owner: 'automation-sovereign',
    primaryDomain: 'c-abi',
    capabilities: ['c-abi-thunk'],
    layer: 'c-abi',
  },
  {
    modulePattern: 'native/include/*',
    owner: 'automation-sovereign',
    primaryDomain: 'c-abi',
    capabilities: ['c-abi-contract'],
    layer: 'c-abi',
  },
  {
    modulePattern: 'src-core/core_system/native/_binding.cpp',
    owner: 'automation-sovereign',
    primaryDomain: 'pybind11-binding',
    capabilities: ['pybind11-binding'],
    layer: 'infrastructure',
  },
  {
    modulePattern: 'main-system/launcher/src/*',
    owner: 'system-runtime-sovereign',
    primaryDomain: 'csharp-adapter',
    capabilities: ['windows-launcher'],
    layer: 'csharp-adapter',
  },
];

export const MAX_CAPABILITIES_PER_OWNER = 3;

// A604 five-core roster — the only valid module owners.
export const CORE_SOVEREIGN_DOMAINS: Record<string, string> = {
  'decision-sovereign': 'policy-architecture',
  'permission-sovereign': 'permission-management',
  'system-runtime-sovereign': 'runtime-management',
  'automation-sovereign': 'synchronization-policy',
  '星澄': 'global-review',
};

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

  if (assignment.capabilities.length > MAX_CAPABILITIES_PER_OWNER) {
    errors.push(`Owner ${assignment.owner} has ${assignment.capabilities.length} capabilities, max is ${MAX_CAPABILITIES_PER_OWNER} (A324)`);
  }

  if (!CORE_SOVEREIGN_DOMAINS[assignment.owner]) {
    errors.push(`Unknown owner: ${assignment.owner}`);
  }

  return errors;
}

function matchPattern(moduleId: string, pattern: string): boolean {
  const regex = pattern
    .replace(/\*/g, '.*')
    .replace(/\?/g, '.');
  return new RegExp(`^${regex}$`).test(moduleId);
}
