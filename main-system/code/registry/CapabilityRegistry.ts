/**
 * CapabilityRegistry.ts — Single explicit capability authority (A356).
 *
 * Contains governance data only. All capability declarations, ownership,
 * and promotion policies live here.
 */

export interface Capability {
  id: string;
  name: string;
  owner: string;                    // Module/sovereign that owns this capability
  language: 'Python' | 'TypeScript' | 'C' | 'C++' | 'CSharp' | 'SQL';
  layer: string;
  latencyBudgetMs?: number;         // For native promotion (A358)
  cpuBudgetPercent?: number;
  memoryBudgetMb?: number;
  throughputTarget?: number;
  status: 'active' | 'deprecated' | 'experimental';
  registeredAt: string;             // ISO timestamp
}

export interface CapabilityRegistryData {
  version: string;
  capabilities: Capability[];
  lastUpdated: string;
}

// Initial registry - populated from sovereign declarations (A334/A356)
export const CAPABILITY_REGISTRY: CapabilityRegistryData = {
  version: '1.0.0',
  lastUpdated: new Date().toISOString(),
  capabilities: [
    // Decision Sovereign capabilities
    {
      id: 'policy-adjudication',
      name: 'Policy Adjudication',
      owner: 'decision-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'priority-delegation',
      name: 'Priority Delegation',
      owner: 'decision-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'change-acceptance',
      name: 'Change Acceptance',
      owner: 'decision-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Permission Sovereign capabilities
    {
      id: 'permission-review',
      name: 'Permission Review',
      owner: 'permission-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'permission-authorize',
      name: 'Permission Authorization',
      owner: 'permission-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'directory-management',
      name: 'Directory Management',
      owner: 'permission-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // System Runtime Sovereign capabilities
    {
      id: 'runtime-lifecycle',
      name: 'Runtime Lifecycle Management',
      owner: 'system-runtime-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'process-survival',
      name: 'Process Survival Monitoring',
      owner: 'system-runtime-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'health-monitoring',
      name: 'Health Monitoring',
      owner: 'system-runtime-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Synchronization Sovereign capabilities
    {
      id: 'sync-priority',
      name: 'Synchronization Priority',
      owner: 'synchronization-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'dependency-order',
      name: 'Dependency Order Resolution',
      owner: 'synchronization-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'conflict-disposition',
      name: 'Conflict Disposition',
      owner: 'synchronization-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Xingcheng Sovereign capabilities
    {
      id: 'global-review',
      name: 'Global System Review',
      owner: 'xingcheng-sovereign',
      language: 'Python',
      layer: 'domain',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'anomaly-classification',
      name: 'Anomaly Classification',
      owner: 'xingcheng-sovereign',
      language: 'Python',
      layer: 'domain',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'user-notification',
      name: 'User Notification',
      owner: 'xingcheng-sovereign',
      language: 'Python',
      layer: 'domain',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Resource Dependency Sync Sub-Sovereign
    {
      id: 'resource-monitoring',
      name: 'Resource State Monitoring',
      owner: 'resource-dependency-sync-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      latencyBudgetMs: 100,
      cpuBudgetPercent: 10,
      memoryBudgetMb: 50,
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'memory-release',
      name: 'Working Set Release',
      owner: 'resource-dependency-sync-sub-sovereign',
      language: 'C++',
      layer: 'native-core',
      latencyBudgetMs: 10,
      cpuBudgetPercent: 5,
      memoryBudgetMb: 10,
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'tool-isolation',
      name: 'Tool Process Isolation',
      owner: 'resource-dependency-sync-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Release Update Sync Sub-Sovereign
    {
      id: 'update-orchestration',
      name: 'Update Orchestration',
      owner: 'release-update-sync-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'hot-reload',
      name: 'Hot Reload Execution',
      owner: 'release-update-sync-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      latencyBudgetMs: 5000,
      cpuBudgetPercent: 20,
      memoryBudgetMb: 200,
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Dependency Sync Sub-Sovereign
    {
      id: 'third-party-inventory',
      name: 'Third-Party Tool Inventory',
      owner: 'dependency-sync-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'version-probing',
      name: 'Version Probing',
      owner: 'dependency-sync-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },

    // Health Maintenance Test Sub-Sovereign
    {
      id: 'connection-watchdog',
      name: 'Connection Health Watchdog',
      owner: 'health-maintenance-test-sub-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'automatic-cleanup',
      name: 'Main-System Internal Automatic Cleanup',
      owner: 'health-maintenance-test-sub-sovereign',
      language: 'Python',
      layer: 'infrastructure',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'auto-repair',
      name: 'Automatic Repair',
      owner: 'health-maintenance-test-sub-sovereign',
      language: 'Python',
      layer: 'application-use-case',
      status: 'active',
      registeredAt: '2026-01-01T00:00:00Z',
    },
  ],
};

export class CapabilityRegistry {
  private registry: CapabilityRegistryData;

  constructor(data: CapabilityRegistryData = CAPABILITY_REGISTRY) {
    this.registry = data;
  }

  getCapability(id: string): Capability | undefined {
    return this.registry.capabilities.find(c => c.id === id);
  }

  getCapabilitiesByOwner(owner: string): Capability[] {
    return this.registry.capabilities.filter(c => c.owner === owner);
  }

  getCapabilitiesByLanguage(language: Capability['language']): Capability[] {
    return this.registry.capabilities.filter(c => c.language === language);
  }

  getCapabilitiesByLayer(layer: string): Capability[] {
    return this.registry.capabilities.filter(c => c.layer === layer);
  }

  getAllCapabilities(): Capability[] {
    return [...this.registry.capabilities];
  }

  registerCapability(capability: Capability): void {
    const existing = this.registry.capabilities.findIndex(c => c.id === capability.id);
    if (existing >= 0) {
      this.registry.capabilities[existing] = capability;
    } else {
      this.registry.capabilities.push(capability);
    }
    this.registry.lastUpdated = new Date().toISOString();
  }

  validateUniqueness(): string[] {
    const errors: string[] = [];
    const seen = new Map<string, string>();

    for (const cap of this.registry.capabilities) {
      if (seen.has(cap.id)) {
        errors.push(`Duplicate capability ID: ${cap.id} (owners: ${seen.get(cap.id)}, ${cap.owner})`);
      }
      seen.set(cap.id, cap.owner);
    }

    return errors;
  }

  toJSON(): string {
    return JSON.stringify(this.registry, null, 2);
  }
}

export const capabilityRegistry = new CapabilityRegistry();