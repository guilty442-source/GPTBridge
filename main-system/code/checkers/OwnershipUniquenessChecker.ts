/**
 * OwnershipUniquenessChecker.ts —Ownership uniqueness verification (A356/A362).
 *
 * Verifies each module maps to exactly one primary sub-sovereign by its
 * dominant state-changing capability and declared ownership.
 */

export type OwnershipVerdict = 'PASS' | 'WARN' | 'FAIL';

export interface ModuleOwnership {
  moduleId: string;
  declaredOwner: string;
  capabilities: string[];
  layer: string;
}

export interface OwnershipViolation {
  rule: string;
  message: string;
  moduleId: string;
  severity: 'error' | 'warn';
}

export interface OwnershipCheckResult {
  verdict: OwnershipVerdict;
  modules: ModuleOwnership[];
  violations: OwnershipViolation[];
  orphanedModules: string[];
  duplicateOwners: string[];
}

export class OwnershipUniquenessChecker {
  private moduleOwnerships: Map<string, ModuleOwnership> = new Map();
  private capabilityRegistry: Map<string, string> = new Map(); // capability -> owner

  registerModule(ownership: ModuleOwnership): void {
    this.moduleOwnerships.set(ownership.moduleId, ownership);
    for (const cap of ownership.capabilities) {
      this.capabilityRegistry.set(cap, ownership.declaredOwner);
    }
  }

  loadFromRegistry(registryData: { capabilities: { id: string; owner: string }[] }): void {
    for (const cap of registryData.capabilities) {
      this.capabilityRegistry.set(cap.id, cap.owner);
    }
  }

  checkOwnership(): OwnershipCheckResult {
    const violations: OwnershipViolation[] = [];
    const ownerModules: Map<string, string[]> = new Map();

    // Check each module has exactly one owner
    for (const [moduleId, ownership] of this.moduleOwnerships) {
      if (!ownership.declaredOwner) {
        violations.push({
          rule: 'owner_required',
          message: `Module ${moduleId} has no declared owner (A356)`,
          moduleId,
          severity: 'error',
        });
      }

      // Track modules per owner
      const modules = ownerModules.get(ownership.declaredOwner) || [];
      modules.push(moduleId);
      ownerModules.set(ownership.declaredOwner, modules);
    }

    // Check for duplicate owners (A356: single owner per module, but owners can have multiple modules)
    const duplicateOwners: string[] = [];
    for (const [owner, modules] of ownerModules) {
      // Per A324: each sub-sovereign manages modules within one exclusive primary domain
      // Multiple modules per owner is allowed if they share the same domain
      const domains = new Set(modules.map(m => this.moduleOwnerships.get(m)?.layer).filter(Boolean));
      if (domains.size > 1) {
        duplicateOwners.push(owner);
        violations.push({
          rule: 'owner_single_domain',
          message: `Owner ${owner} spans multiple domains: ${Array.from(domains).join(', ')} (A324)`,
          moduleId: modules.join(', '),
          severity: 'warn',
        });
      }
    }

    // Check capability ownership uniqueness (A356)
    const capabilityOwners = new Map<string, string>();
    for (const [capability, owner] of this.capabilityRegistry) {
      if (capabilityOwners.has(capability)) {
        violations.push({
          rule: 'capability_unique_owner',
          message: `Capability ${capability} has multiple owners: ${capabilityOwners.get(capability)}, ${owner}`,
          moduleId: capability,
          severity: 'error',
        });
      }
      capabilityOwners.set(capability, owner);
    }

    // Check orphaned modules (no owner in capability registry)
    const orphanedModules: string[] = [];
    for (const [moduleId, ownership] of this.moduleOwnerships) {
      const hasRegisteredCapability = ownership.capabilities.some(c => this.capabilityRegistry.has(c));
      if (!hasRegisteredCapability && ownership.capabilities.length > 0) {
        orphanedModules.push(moduleId);
        violations.push({
          rule: 'module_orphaned',
          message: `Module ${moduleId} declares capabilities not in registry`,
          moduleId,
          severity: 'warn',
        });
      }
    }

    const verdict = violations.some(v => v.severity === 'error') ? 'FAIL' :
                    violations.some(v => v.severity === 'warn') ? 'WARN' : 'PASS';

    return {
      verdict,
      modules: Array.from(this.moduleOwnerships.values()),
      violations,
      orphanedModules,
      duplicateOwners,
    };
  }
}

export function runOwnershipUniquenessGate(
  modules: ModuleOwnership[],
  capabilityRegistry: { capabilities: { id: string; owner: string }[] }
): OwnershipCheckResult {
  const checker = new OwnershipUniquenessChecker();
  for (const m of modules) checker.registerModule(m);
  checker.loadFromRegistry(capabilityRegistry);
  return checker.checkOwnership();
}