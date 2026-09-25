/**
 * LanguagePolicy.ts — Canonical language policy (A219/A211/A215/A354).
 *
 * Immutable data only. Single source of truth for language governance.
 */

export const LANGUAGE_ROLES = {
  C: 'public native interface only+stable ABI',
  Cpp: 'private high-load implementation+measured-performance-critical',
  CSharp: 'Windows-specific .NET/CLR/WinRT/COM integration only',
  FSharp: 'data analysis+machine learning+high-correctness complex calculation',
  Go: 'bounded concurrent services+transport workers+network adapters+portable operational binaries through versioned contracts',
  Rust: 'memory-safe systems capabilities+parsers+integrity/security-sensitive native components through versioned C ABI or typed service contracts',
  TypeScript: 'UI presentation/client+contract+transport+type-safety+governance-checker',
  SQL: 'relational set operations+data selection+projection',
  Python: 'on-demand governance semantics+model research/training+bounded boundaries',
} as const;

// Primary format: one language = one canonical authored-source format
// (codex direction: C-family primary stack, single-format convergence).
export const PRIMARY_FORMAT = {
  C: '.c',
  Cpp: '.cpp',
  CSharp: '.cs',
  FSharp: '.fs',
  Go: '.go',
  Rust: '.rs',
  TypeScript: '.ts',
  SQL: '.sql',
  Python: '.py',
} as const;

// Grandfathered alternates: existing files pinned, new authored files denied.
export const GRANDFATHERED_EXTENSIONS = {
  Python: ['.pyi'] as const,
  TypeScript: ['.tsx', '.d.ts'] as const,
  C: ['.h'] as const,
  Cpp: ['.hpp', '.inl'] as const,
  CSharp: [] as const,
  FSharp: ['.fsx'] as const,
  Go: [] as const,
  Rust: [] as const,
  SQL: [] as const,
} as const;

export const CANONICAL_EXTENSIONS = {
  Python: ['.py', '.pyi'] as const,
  TypeScript: ['.ts', '.tsx', '.d.ts'] as const,
  C: ['.c', '.h'] as const,
  Cpp: ['.cpp', '.hpp', '.inl'] as const,
  CSharp: ['.cs'] as const,
  FSharp: ['.fs', '.fsx'] as const,
  Go: ['.go'] as const,
  Rust: ['.rs'] as const,
  SQL: ['.sql'] as const,
} as const;

export const SOLE_MAPPING = {
  Python: 'system-control+semantic/business logic',
  TypeScript: 'UI presentation/client',
  C: 'public native interface only',
  Cpp: 'measured-performance-critical deterministic-native-compute+algorithms+memory',
  CSharp: 'Windows-specific .NET/CLR/WinRT/COM integration that cannot-be-provided-by-existing-Python/TypeScript/C/C++ owner without-loss',
  FSharp: 'data analysis+machine learning+high-correctness complex calculation that cannot-be-provided-by-existing-Python/C/C++/CSharp owner without-loss',
  Go: 'bounded concurrent services+transport workers+network adapters+portable operational binaries that cannot-be-provided-by-existing-owners without-loss',
  Rust: 'memory-safe systems capabilities+parsers+integrity/security-sensitive native components that cannot-be-provided-by-existing-owners without-loss',
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
  FSharp: ['Python', 'SQL', 'UI'],
  Go: ['domain', 'SQL', 'native', 'UI'],
  Rust: ['domain', 'SQL', 'UI'],
} as const;

export const SOURCE_ROOTS = {
  Python: ['main-system/src-core/', 'shared-layer/src/', 'governance/', 'Standalone tools/'],
  TypeScript: ['main-system/src-ui/', 'main-system/scripts/'],
  C: ['native/include/', 'native/bridge/'],
  Cpp: ['native/core/', 'native/bridge/'],
  CSharp: [
    'main-system/launcher/src/',
    'Standalone tools/business-logic-csharp/',
    'Standalone tools/process-metrics-csharp/',
  ],
  FSharp: ['Standalone tools/'],
  Go: ['Standalone tools/'],
  Rust: ['Standalone tools/', 'native/'],
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
  const lower = filePath.toLowerCase();
  // A215: longest-suffix-first so .d.ts resolves as TypeScript declaration,
  // not generic .ts; then casefolded suffix match.
  const entries = Object.entries(CANONICAL_EXTENSIONS) as Array<
    [Language, readonly string[]]
  >;
  const candidates: Array<{ lang: Language; ext: string }> = [];
  for (const [lang, exts] of entries) {
    for (const ext of exts) {
      if (lower.endsWith(ext)) candidates.push({ lang, ext });
    }
  }
  candidates.sort((a, b) => b.ext.length - a.ext.length);
  return candidates.length ? candidates[0].lang : null;
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