/**
 * DependencyPolicy.ts ?ependency governance policy (A68/A354/A362).
 *
 * Immutable data only. Defines layer architecture and dependency rules.
 */

export const LAYER_HIERARCHY = [
  'presentation',           // TypeScript UI, Electron main
  'channel-api',            // Information layer contracts, IPC, events
  'application-use-case',   // Python orchestration, workflows, use cases
  'domain',                 // Python domain policy, business logic
  'infrastructure',         // Python adapters, native bindings, SQL, tools
  'native-core',            // C++ private implementation (parser, vector, memory, token, transform, binding)
  'c-abi',                  // C public interface (gptbridge_native.h)
  'csharp-adapter',         // C# Windows/.NET adapter
] as const;

export type Layer = typeof LAYER_HIERARCHY[number];

export const LAYER_RULES: Record<Layer, { allowedDeps: Layer[]; description: string }> = {
  'presentation': {
    allowedDeps: ['channel-api'],
    description: 'TypeScript UI may ONLY depend on channel-api contracts (A351)',
  },
  'channel-api': {
    allowedDeps: [],
    description: 'Information layer contracts have no internal dependencies',
  },
  'application-use-case': {
    allowedDeps: ['channel-api', 'domain', 'infrastructure', 'c-abi', 'csharp-adapter'],
    description: 'Python orchestration depends on channel-api, domain, infrastructure, and adapters',
  },
  'domain': {
    allowedDeps: ['infrastructure'],
    description: 'Domain policy depends on infrastructure for data access',
  },
  'infrastructure': {
    allowedDeps: ['c-abi', 'native-core'],
    description: 'Adapters depend on C ABI and native core',
  },
  'native-core': {
    allowedDeps: [],
    description: 'C++ core has no internal dependencies; called via C ABI',
  },
  'c-abi': {
    allowedDeps: [],
    description: 'C ABI is the boundary; no further dependencies',
  },
  'csharp-adapter': {
    allowedDeps: ['c-abi'],
    description: 'C# adapter calls through C ABI only',
  },
};

// A604 five-core roster: modules are owned directly by a peer core
// (the sub-sovereign layer is eliminated).
export const MODULE_OWNERSHIP = {
  'decision-sovereign': 'decision-sovereign',
  'permission-sovereign': 'permission-sovereign',
  'system-runtime-sovereign': 'system-runtime-sovereign',
  'automation-sovereign': 'automation-sovereign',
  '星澄': '星澄',
  'hot-update': 'automation-sovereign',
  'tool-isolation': 'automation-sovereign',
  'update-manager': 'automation-sovereign',
  'connection-watchdog': 'decision-sovereign',
  'hot-reload': 'decision-sovereign',
  'automatic-cleanup': 'automation-sovereign',
  'auto-repair': 'decision-sovereign',
} as const;

export const SOVEREIGN_LAYERS: Record<string, Layer> = {
  'decision-sovereign': 'application-use-case',
  'permission-sovereign': 'application-use-case',
  'system-runtime-sovereign': 'application-use-case',
  'automation-sovereign': 'application-use-case',
  '星澄': 'domain',
} as const;

export function validateDependency(fromLayer: Layer, toLayer: Layer): { valid: boolean; reason?: string } {
  const rules = LAYER_RULES[fromLayer];
  if (!rules) return { valid: false, reason: `Unknown layer: ${fromLayer}` };

  if (!rules.allowedDeps.includes(toLayer)) {
    return {
      valid: false,
      reason: `${fromLayer} cannot depend on ${toLayer}. ${rules.description} (A68)`
    };
  }

  return { valid: true };
}

export function getLayerForModule(moduleId: string): Layer | null {
  if (SOVEREIGN_LAYERS[moduleId]) return SOVEREIGN_LAYERS[moduleId];

  // Heuristic based on path
  if (moduleId.includes('src-ui') || moduleId.includes('renderer')) return 'presentation';
  if (moduleId.includes('channel') || moduleId.includes('contract') || moduleId.includes('ipc')) return 'channel-api';
  if (moduleId.includes('orchestrat') || moduleId.includes('workflow')) return 'application-use-case';
  if (moduleId.includes('domain') || moduleId.includes('policy')) return 'domain';
  if (moduleId.includes('adapter') || moduleId.includes('binding') || moduleId.includes('native')) return 'infrastructure';
  if (moduleId.includes('native/core')) return 'native-core';
  if (moduleId.includes('native/include') || moduleId.includes('native/bridge')) return 'c-abi';
  if (moduleId.includes('launcher/src')) return 'csharp-adapter';

  return null;
}