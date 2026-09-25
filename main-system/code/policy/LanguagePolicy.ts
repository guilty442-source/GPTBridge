/**
 * LanguagePolicy.ts — Canonical language policy (A219/A211/A215/A354).
 *
 * Immutable data only. Single source of truth for language governance.
 */

// A35 (codex 2026-09-25, final-language-and-package-division):
// PRIMARY-EXECUTION-ORDER c23>c++23>csharp14; TypeScript retired->JavaScript-ESM.
export const LANGUAGE_ROLES = {
  C: 'runtime core+permission hot paths+deterministic execution+low-level computation+stable ABI',
  Cpp: 'model inference+native tool runtime+audit engine+private high-load implementation',
  CSharp: 'application+workflow+API+Windows .NET integration+sole test orchestration on .NET 10',
  FSharp: 'core business logic+data validation+transformation+business state transitions',
  Go: 'xingcheng web search+batch processing+network and file I/O bounded concurrent services',
  Rust: 'local vector engine+RAG retrieval+native security+desktop host',
  JavaScript: 'React UI presentation/client+frontend state+desktop interaction (ESM)',
  Julia: 'statistics+mathematical models+optimization+simulation+scientific computation',
  SQL: 'set-based data operations within the PostgreSQL sole authority',
  Python: 'bounded governance semantics+governance thin-wrapper+JAX training+necessary validation only+nonresident after request completion',
  TypeScript: 'RETIRED — succeeded by JavaScript-ESM; grandfathered existing authored files only',
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
  JavaScript: '.js',
  Julia: '.jl',
  SQL: '.sql',
  Python: '.py',
  // TypeScript retired — no canonical new-authored format.
} as const;

// Grandfathered alternates: existing files pinned, new authored files denied.
// A348: typescript=.ts,.tsx,.d.ts (retired); javascript=.jsx,.mjs,.cjs.
export const GRANDFATHERED_EXTENSIONS = {
  Python: ['.pyi'] as const,
  TypeScript: ['.ts', '.tsx', '.d.ts'] as const,
  JavaScript: ['.jsx', '.mjs', '.cjs'] as const,
  C: ['.h'] as const,
  Cpp: ['.hpp', '.inl'] as const,
  CSharp: [] as const,
  FSharp: ['.fsx'] as const,
  Go: [] as const,
  Rust: [] as const,
  Julia: [] as const,
  SQL: [] as const,
} as const;

export const CANONICAL_EXTENSIONS = {
  Python: ['.py', '.pyi'] as const,
  TypeScript: ['.ts', '.tsx', '.d.ts'] as const,
  JavaScript: ['.js', '.jsx', '.mjs', '.cjs'] as const,
  Julia: ['.jl'] as const,
  C: ['.c', '.h'] as const,
  Cpp: ['.cpp', '.hpp', '.inl'] as const,
  CSharp: ['.cs'] as const,
  FSharp: ['.fs', '.fsx'] as const,
  Go: ['.go'] as const,
  Rust: ['.rs'] as const,
  SQL: ['.sql'] as const,
} as const;

export const SOLE_MAPPING = {
  Python: 'bounded governance semantics+governance thin-wrapper+JAX training and necessary validation',
  JavaScript: 'React UI presentation/client+frontend state+desktop interaction',
  Julia: 'statistics+mathematical models+optimization+simulation+scientific computation',
  C: 'runtime core+permission hot paths+deterministic execution+low-level computation',
  Cpp: 'model inference+native tool runtime+audit engine',
  CSharp: 'application+workflow+API+Windows/.NET integration+sole test orchestration',
  FSharp: 'core business logic+data validation+transformation+business state transitions',
  Go: 'xingcheng web search+batch processing+network and file I/O',
  Rust: 'local vector engine+RAG retrieval+native security+desktop host',
  TypeScript: 'retired lineage succeeded by JavaScript-ESM (existing authored files grandfathered)',
} as const;

export const NATIVE_BOUNDARY_FORMS = [
  'C-ABI-language-neutral',
  'pybind11-Python-specific',
] as const;

// A352 verdict matrix: listed edges are FAIL; fail-closed, no auto-fallback.
export const FORBIDDEN_CROSS_BOUNDARIES = {
  JavaScript: ['database', 'Python-import', 'native', 'process-spawn', 'Worker-dom-storage', 'filesystem-bridge-Python'],
  TypeScript: ['new-authored'], // retired: any newly authored .ts/.tsx/.d.ts is FAIL
  Python: ['SQL', 'UI', 'DOM', 'frontend-storage', 'UI-framework'],
  SQL: ['Python', 'UI'],
  UI: ['Python', 'SQL', 'CSharp'],
  Cpp: ['Python', 'SQL', 'UI'],
  CSharp: ['SQL', 'UI', 'domain', 'native'],
  FSharp: ['SQL', 'UI', 'Python-direct'],
  Go: ['domain', 'SQL', 'UI', 'permission', 'governance', 'private-bridge'],
  Rust: ['domain', 'SQL', 'UI', 'permission', 'governance'],
  Julia: ['governance', 'UI', 'SQL'],
} as const;

export const SOURCE_ROOTS = {
  Python: ['main-system/src-core/', 'shared-layer/src/', 'governance/', 'Standalone tools/'],
  JavaScript: ['main-system/src-ui/', 'main-system/scripts/'],
  TypeScript: ['main-system/src-ui/', 'main-system/scripts/'], // grandfathered files only
  Julia: ['Standalone tools/'],
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

// A343: execution modules prefer C/C++ then C#; Python bounded on-demand
export const CORE_ORDER = [
  'correctness-first',
  'declared-language-owner',
  'representative reproducible profile',
  'contract and behavior oracle',
  'independent acceptance',
  'certified activation',
] as const;

export type Language = keyof typeof LANGUAGE_ROLES;
export type Layer = 'presentation' | 'channel-api' | 'application-use-case' | 'domain' | 'infrastructure' | 'native-core' | 'c-abi' | 'csharp-adapter' | 'fsharp-analysis' | 'go-service' | 'rust-component';

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