/**
 * LanguagePolicy.js — C55 governed language boundary policy.
 *
 * Immutable declarative data only. No scanning, resolution, filesystem,
 * network, process, evidence, verdict or repair behavior may live here.
 * Consumers: checkers/LanguageBoundaryChecker.js (READONLY-V1).
 */

const freeze = (o) => Object.freeze(o);
const freezeDeep = (o) => {
  for (const v of Object.values(o)) {
    if (v && typeof v === 'object') freezeDeep(v);
  }
  return freeze(o);
};

// Active language roster per B166/P115 (rev 197): python and typescript
// are retired — they classify, but no active role or authored extension
// remains.
export const LANGUAGE_ROLES = freezeDeep({
  c: 'deterministic-runtime+stable-abi+permission-hot-paths',
  cpp: 'native-inference+native-tool-runtime+audit+model-training',
  csharp: 'application+workflow+api+windows-dotnet+sole-test-orchestration',
  fsharp: 'business-logic+validation+analysis+eval-verdict',
  rust: 'ui-core-state-lifecycle+ipc+security+os+vector-engine+rag',
  go: 'network-and-batch-services',
  'javascript-esm': 'general-ui+registered-governance-checker-subtree',
  julia: 'scientific-compute',
  sql: 'postgresql-set-based-data',
  python: 'retired',
  typescript: 'retired',
});

// Canonical authored-source extensions per language. Retired languages
// keep a classification entry (historical files classify, then fail the
// retired-language gate) but own no admitted authored extension.
export const CANONICAL_EXTENSIONS = freezeDeep({
  c: ['.c', '.h'],
  cpp: ['.cpp', '.hpp', '.inl'],
  csharp: ['.cs'],
  fsharp: ['.fs'],
  rust: ['.rs'],
  go: ['.go'],
  'javascript-esm': ['.js'],
  julia: ['.jl'],
  sql: ['.sql'],
  python: ['.py', '.pyi'],
  typescript: ['.ts', '.tsx', '.d.ts'],
});

// Extensions that may still exist as pinned historical artifacts; a new
// authored file carrying one of these is a retired-language violation.
export const GRANDFATHERED_EXTENSIONS = freezeDeep({
  'javascript-esm': ['.jsx', '.mjs', '.cjs'],
  fsharp: ['.fsx'],
});

// B75 SOURCE-ORIGIN vocabulary.
export const SOURCE_ORIGINS = freeze(['AUTHORED', 'GENERATED', 'THIRD_PARTY']);

// Retired languages: classification only, authored source denied.
export const RETIRED_LANGUAGES = freeze(['python', 'typescript']);

// Directory allowlist per language (B75). The javascript-esm entry
// includes this registered governance checker subtree per the C55
// DIRECTORY-EXCEPTION.
export const SOURCE_ROOTS = freezeDeep({
  c: ['native/include/', 'native/bridge/', 'native/core/', 'native/resource_governor/'],
  cpp: ['native/core/', 'native/bridge/', 'native/resource_governor/', 'Standalone tools/'],
  csharp: ['main-system/', 'shared-layer/csharp/', 'Standalone tools/'],
  fsharp: ['Standalone tools/', 'shared-layer/fsharp/'],
  rust: ['Standalone tools/', 'native/'],
  go: ['Standalone tools/'],
  'javascript-esm': [
    'main-system/src-ui/',
    'Standalone tools/model-dialogue/',
    'governance_rule/execution/code/',
  ],
  julia: ['Standalone tools/'],
  sql: ['governance_rule/', 'main-system/', 'shared-layer/sql/'],
  python: [],
  typescript: [],
});

// A352-style forbidden cross-boundary edges. Post-retirement: no live
// language may edge to python or typescript; the reverse direction is
// dead because retired languages own no authored files.
export const FORBIDDEN_CROSS_BOUNDARIES = freezeDeep({
  'javascript-esm': [
    'database',
    'python',
    'typescript',
    'native',
    'process-spawn',
    'worker-dom-storage',
    'filesystem-bridge',
  ],
  sql: ['python', 'ui'],
  cpp: ['python', 'sql', 'ui'],
  c: ['python', 'sql', 'ui'],
  csharp: ['domain', 'sql', 'ui', 'native', 'python', 'typescript'],
  fsharp: ['sql', 'ui', 'python', 'typescript'],
  go: ['domain', 'sql', 'ui', 'permission', 'governance', 'private-bridge', 'python', 'typescript'],
  rust: ['domain', 'sql', 'ui', 'permission', 'governance', 'python', 'typescript'],
  julia: ['governance', 'ui', 'sql', 'python', 'typescript'],
});

// B32 canonical contract: javascript-esm -> information_layer expands to
// the validated-shell-bridge flow; no python-qualified expansion exists.
export const CANONICAL_QUALIFICATIONS = freezeDeep({
  'javascript-esm->information_layer': 'B32:validated-shell-bridge>typed-application-core>information-layer',
});

export default freezeDeep({
  LANGUAGE_ROLES,
  CANONICAL_EXTENSIONS,
  GRANDFATHERED_EXTENSIONS,
  SOURCE_ORIGINS,
  RETIRED_LANGUAGES,
  SOURCE_ROOTS,
  FORBIDDEN_CROSS_BOUNDARIES,
  CANONICAL_QUALIFICATIONS,
});
