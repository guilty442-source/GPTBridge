/**
 * LanguagePolicy.ts ?�Canonical language policy (A219/A211/A215/A354).
 *
 * Immutable data only. Single source of truth for language governance.
 */

export const LANGUAGE_ROLES = {
  Python: 'system-control+semantic/business logic+orchestration+governed-workflow+adapter-coordination',
  TypeScript: 'UI presentation/client+contract+transport+type-safety+governance-checker',
  C: 'public native interface only+stable ABI',
  Cpp: 'private high-load implementation+measured-performance-critical',
  CSharp: 'Windows-specific .NET/CLR/WinRT/COM integration only',
  SQL: 'relational set operations+data selection+projection',
} as const;

export const CANONICAL_EXTENSIONS = {
  Python: ['.py', '.pyi'] as const,
  TypeScript: ['.ts', '.tsx', '.d.ts'] as const,
  C: ['.c', '.h'] as const,
  Cpp: ['.cpp', '.hpp', '.inl'] as const,
  CSharp: ['.cs'] as const,
  SQL: ['.sql'] as const,
} as const;

export const SOLE_MAPPING = {
  Python: 'system-control+semantic/business logic',
  TypeScript: 'UI presentation/client',
  C: 'public native interface only',
  Cpp: 'measured-performance-critical deterministic-native-compute+algorithms+memory',
  CSharp: 'Windows-specific .NET/CLR/WinRT/COM integration that cannot-be-provided-by-existing-Python/TypeScript/C/C++ owner without-loss',
} as const;

export const NATIVE_BOUNDARY_FORMS = [
  'C-ABI-language-neutral',
  'pybind11-Python-specific',
] as const;

export const FORBIDDEN_CROSS_BOUNDARIES = {
  TypeScript: ['Python-import', 'database', 'native', 'CSharp', 'SQL-direct-connection', 'DOM', 'frontend-storage', 'UI-framework'],
  Python: ['DOM', 'frontend-storage', 'UI-framework'],
  Cpp: ['Python', 'SQL', 'UI'],
  CSharp: ['domain', 'SQL', 'native'],
} as const;

export const SOURCE_ROOTS = {
  Python: ['main-system/src-core/', 'shared-layer/src/', 'governance/', 'Standalone tools/'],
  TypeScript: ['main-system/src-ui/', 'main-system/scripts/'],
  C: ['native/include/', 'native/bridge/'],
  Cpp: ['native/core/', 'native/bridge/'],
  CSharp: ['main-system/launcher/src/'],
  SQL: ['governance/', 'main-system/src-core/'],
} as const;

export const PYTHON_INTERNAL_LAYERS = [
  'application',
  'domain',
  'infrastructure',
] as const;

export const CORE_ORDER = [
  'correctness-first',
  'Python authoritative implementation',
  'representative reproducible profile',
  'bottleneck confirmed',
  'C++ optimization candidate',
  'API/behavior/performance comparison',
  'independent acceptance',
] as const;

export type Language = keyof typeof LANGUAGE_ROLES;
export type Layer = 'presentation' | 'channel-api' | 'application-use-case' | 'domain' | 'infrastructure' | 'native-core' | 'c-abi' | 'csharp-adapter';

export function getLanguageForFile(filePath: string): Language | null {
  const ext = filePath.substring(filePath.lastIndexOf('.'));
  const entries = Object.entries(CANONICAL_EXTENSIONS) as Array<
    [Language, readonly string[]]
  >;
  for (const [lang, exts] of entries) {
    if (exts.includes(ext)) return lang;
  }
  return null;
}

export function isValidLocation(filePath: string, language: Language): boolean {
  const allowed = SOURCE_ROOTS[language];
  return allowed.some(root => filePath.replace(/\\/g, '/').startsWith(root));
}

export function isCrossLanguageAllowed(from: Language, to: Language): boolean {
  const forbidden: readonly string[] | undefined =
    FORBIDDEN_CROSS_BOUNDARIES[from];
  if (!forbidden) return true;
  return !forbidden.includes(to);
}