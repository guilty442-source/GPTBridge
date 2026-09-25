/**
 * LanguageBoundaryChecker.ts — Language Boundary Gate (A348/A351/A352/A353).
 *
 * VERDICT: exactly PASS | WARN | FAIL
 * TRIGGERS: save > pre-commit > integration > release
 * CHECKS: language_allowed + extension_valid + source_origin_valid + owner_valid + directory_valid + cross_boundary_deny
 */

export type LanguageVerdict = 'PASS' | 'WARN' | 'FAIL';

export interface LanguageBoundaryResult {
  verdict: LanguageVerdict;
  file: string;
  language: string;
  violations: LanguageViolation[];
  warnings: string[];
}

export interface LanguageViolation {
  rule: string;
  message: string;
  line?: number;
  severity: 'error' | 'warn';
}

export interface LanguagePolicy {
  allowedLanguages: string[];
  canonicalRoles: Map<string, string>;
  forbiddenCrossBoundaries: CrossBoundaryRule[];
  // A348: retired languages deny new-authored files; pinned grandfathered
  // paths remain legal until migrated through scripts/ts_to_esm.mjs.
  retiredLanguages?: string[];
  grandfatheredPaths?: ReadonlySet<string>;
}

export interface CrossBoundaryRule {
  from: string;
  to: string[];
  reason: string;
}

// Canonical language configuration per A219, A211, A215, A264
// A348: 10 allowed languages — TypeScript retired (grandfathered only),
// JavaScript-ESM and Julia admitted (final-language-and-package-division).
export const LANGUAGE_POLICY: LanguagePolicy = {
  allowedLanguages: ['Python', 'JavaScript', 'Julia', 'C', 'C++', 'CSharp', 'FSharp', 'Go', 'Rust', 'SQL'],
  retiredLanguages: ['TypeScript'],
  canonicalRoles: new Map([
    // A219, A211
    ['Python', 'bounded governance semantics+governance thin-wrapper+JAX training+necessary validation+nonresident'],
    ['JavaScript', 'React UI presentation/client+frontend state+desktop interaction (ESM)'],
    ['Julia', 'statistics+mathematical models+optimization+simulation+scientific computation'],
    ['C', 'runtime core+permission hot paths+deterministic execution+stable ABI'],
    ['C++', 'model inference+native tool runtime+audit engine+private implementation'],
    ['CSharp', 'application+workflow+API+Windows .NET integration+sole test orchestration on .NET 10'],
    ['FSharp', 'core business logic+data validation+transformation+business state transitions'],
    ['Go', 'xingcheng web search+batch processing+network and file I/O'],
    ['Rust', 'local vector engine+RAG retrieval+native security+desktop host'],
    ['SQL', 'relational set operations+data selection+projection within PostgreSQL sole authority'],
    ['TypeScript', 'RETIRED — succeeded by JavaScript-ESM; grandfathered existing files only'],
  ]),
  forbiddenCrossBoundaries: [
    // A351, A352
    { from: 'JavaScript', to: ['database', 'Python-import', 'native', 'process-spawn', 'Worker-dom-storage'], reason: 'JavaScript owns React UI only; no backend/data/native/process edges' },
    { from: 'Python', to: ['SQL', 'UI', 'DOM', 'frontend-storage', 'UI-framework'], reason: 'Python MUST NOT access data/UI surfaces directly' },
    { from: 'SQL', to: ['Python', 'UI'], reason: 'SQL bounded data operations only' },
    { from: 'C++', to: ['Python', 'SQL', 'UI'], reason: 'C++ private implementation only; exposed via C ABI' },
    { from: 'CSharp', to: ['domain', 'SQL', 'UI', 'native'], reason: 'CSharp orchestration/adapter only' },
    { from: 'FSharp', to: ['SQL', 'UI', 'Python'], reason: 'FSharp business-logic capability only' },
    { from: 'Go', to: ['domain', 'SQL', 'UI', 'permission', 'governance'], reason: 'Go bounded service only; no governance/permission/business judgment' },
    { from: 'Rust', to: ['domain', 'SQL', 'UI', 'permission', 'governance'], reason: 'Rust memory-safe systems capability only; no governance/permission/business judgment' },
    { from: 'Julia', to: ['governance', 'UI', 'SQL'], reason: 'Julia scientific-compute capability only' },
  ],
};

// Canonical file extensions per A215 (primary format first; alternates
// grandfathered). TypeScript stays classified so existing files are pinned,
// not "unknown"; JavaScript .jsx/.mjs/.cjs are grandfathered alternates.
export const CANONICAL_EXTENSIONS = {
  Python: ['.py', '.pyi'],
  TypeScript: ['.ts', '.tsx', '.d.ts'],
  JavaScript: ['.js', '.jsx', '.mjs', '.cjs'],
  Julia: ['.jl'],
  C: ['.c', '.h'],
  Cpp: ['.cpp', '.hpp', '.inl'],
  CSharp: ['.cs'],
  FSharp: ['.fs', '.fsx'],
  Go: ['.go'],
  Rust: ['.rs'],
  SQL: ['.sql'],
};

export class LanguageBoundaryChecker {
  private policy: LanguagePolicy;

  constructor(policy: LanguagePolicy = LANGUAGE_POLICY) {
    this.policy = policy;
  }

  checkFile(filePath: string, content: string): LanguageBoundaryResult {
    const violations: LanguageViolation[] = [];
    const warnings: string[] = [];
    const language = this.detectLanguage(filePath);

    if (!language) {
      return {
        verdict: 'FAIL',
        file: filePath,
        language: 'unknown',
        violations: [{ rule: 'language_allowed', message: `Unknown language for file: ${filePath}`, severity: 'error' }],
        warnings: [],
      };
    }

    // Check extension validity
    if (!this.isValidExtension(filePath, language)) {
      violations.push({
        rule: 'extension_valid',
        message: `Invalid extension for ${language}: ${filePath}`,
        severity: 'error',
      });
    }

    // A348: retired language denies new-authored files; pinned
    // grandfathered paths stay legal until migrated.
    if (this.policy.retiredLanguages?.includes(language)) {
      if (!this.isGrandfathered(filePath.replace(/\\/g, '/'))) {
        violations.push({
          rule: 'language_allowed',
          message: language + ' retired (A348 -> JavaScript-ESM); new-authored file denied -- migrate via main-system/scripts/ts_to_esm.mjs',
          severity: 'error',
        });
      }
    }

    // Check cross-boundary imports
    const importViolations = this.checkImports(filePath, content, language);
    violations.push(...importViolations);

    // Check directory validity
    const dirViolation = this.checkDirectory(filePath, language);
    if (dirViolation) violations.push(dirViolation);

    const verdict = violations.some(v => v.severity === 'error') ? 'FAIL' :
                    violations.some(v => v.severity === 'warn') ? 'WARN' : 'PASS';

    return { verdict, file: filePath, language, violations, warnings };
  }

  // A215: longest-suffix-first canonical extension (e.g. .d.ts before .ts)
  private canonicalExtension(filePath: string): string | null {
    const lower = filePath.toLowerCase();
    const all = Object.values(CANONICAL_EXTENSIONS).flat() as string[];
    const match = all
      .filter(ext => lower.endsWith(ext))
      .sort((a, b) => b.length - a.length)[0];
    return match ?? null;
  }

  private detectLanguage(filePath: string): string | null {
    const ext = this.canonicalExtension(filePath);
    if (!ext) return null;
    for (const [lang, exts] of Object.entries(CANONICAL_EXTENSIONS)) {
      if ((exts as readonly string[]).includes(ext)) return lang;
    }
    return null;
  }

  private isValidExtension(filePath: string, language: string): boolean {
    const ext = this.canonicalExtension(filePath);
    const validExts = CANONICAL_EXTENSIONS[language as keyof typeof CANONICAL_EXTENSIONS] || [];
    return ext !== null && (validExts as readonly string[]).includes(ext);
  }

  /** Repo-relative baseline suffix match: callers may hand cwd-relative or
   * root-relative paths; a pinned path is grandfathered when it equals or
   * suffix-matches the normalized path in either direction. */
  private isGrandfathered(normalized: string): boolean {
    const baseline = this.policy.grandfatheredPaths;
    if (!baseline || baseline.size === 0) return false;
    if (baseline.has(normalized)) return true;
    for (const pinned of baseline) {
      if (normalized.endsWith('/' + pinned) || pinned.endsWith('/' + normalized)) {
        return true;
      }
    }
    return false;
  }

  private checkImports(filePath: string, content: string, language: string): LanguageViolation[] {
    const violations: LanguageViolation[] = [];
    const lines = content.split('\n');

    const forbiddenRules = this.policy.forbiddenCrossBoundaries.find(r => r.from === language);
    if (!forbiddenRules) return violations;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line || line.startsWith('//')) continue;

      for (const forbidden of forbiddenRules.to) {
        if (this.lineContainsForbiddenImport(line, forbidden, language)) {
          violations.push({
            rule: 'cross_boundary_deny',
            message: `${language} cannot import/use ${forbidden} (A351/A352)`,
            line: i + 1,
            severity: 'error',
          });
        }
      }
    }
    return violations;
  }

  private lineContainsForbiddenImport(line: string, forbidden: string, language: string): boolean {
    const patterns: Record<string, RegExp[]> = {
      'Python-import': [
        /import\s+[\w.]+\.py/,
        /from\s+[\w.]+\.py\s+import/,
      ],
      'database': [
        /import\s+(sqlite3|psycopg2|sqlalchemy|postgresql)/,
        /from\s+(sqlite3|psycopg2|sqlalchemy|postgresql)\s+import/,
      ],
      'native': [
        /import\s+(ctypes|ctypes\.|ctypes\.)/,
        /from\s+ctypes\s+import/,
        /import\s+_sovereign_native/,
        /import\s+_binding/,
        /require\(['"]native['"]\)/,
        /require\(['"]ffi['"]\)/,
        /require\(['"]addon['"]\)/,
      ],
      'CSharp': [
        /using\s+System/,
        /using\s+Microsoft/,
        /using\s+CSharp/,
      ],
      'SQL-direct-connection': [
        /import\s+(sqlite3|psycopg2)/,
        /\.execute\(/,
        /\.query\(/,
      ],
      'DOM': [
        /document\./,
        /window\./,
        /\.querySelector/,
        /\.getElementById/,
      ],
      'frontend-storage': [
        /localStorage/,
        /sessionStorage/,
        /indexedDB/,
      ],
      'UI-framework': [
        /import\s+(tkinter|PyQt5|PyQt6|PySide2|PySide6|wx|kivy)/,
      ],
      'process-spawn': [
        /\brequire\(['"](?:node:)?child_process['"]\)/,
        /\bfrom\s+['"](?:node:)?child_process['"]/,
        /\bspawn(?:Sync)?\s*\(/,
        /\bexec(?:File|Sync)?\s*\(/,
      ],
      'Worker-dom-storage': [
        /\bnew\s+Worker\s*\(/,
        /localStorage/,
        /sessionStorage/,
        /indexedDB/,
      ],
      'permission': [
        /\bpermission[-_]?sovereign\b/,
        /\bPERMISSION_[A-Z_]+\b/,
      ],
      'governance': [
        /\bgovernance\b/,
        /\bcodex\b/i,
      ],
      'new-authored': [],
      'UI': [
        /import\s+(tkinter|PyQt5|PyQt6|PySide2|PySide6|wx|kivy)/,
        /document\.|window\.|\.querySelector|\.getElementById/,
      ],
      'domain': [],
      'Python': [
        /\bimport\s+(sqlite3|psycopg2|sqlalchemy)/,
        /\bfrom\s+(sqlite3|psycopg2|sqlalchemy)\s+import/,
        /\bimport\s+(tkinter|PyQt5|PyQt6|PySide2|PySide6)/,
      ],
      'SQL': [
        /\bimport\s+(sqlite3|psycopg2|sqlalchemy|psycopg)/,
        /\bfrom\s+(sqlite3|psycopg2|sqlalchemy|psycopg)\s+import/,
        /\.execute\s*\(|\.query\s*\(/,
      ],
    };

    const checkPatterns = patterns[forbidden] || [];
    return checkPatterns.some(p => p.test(line));
  }

  private checkDirectory(filePath: string, language: string): LanguageViolation | null {
    const normalized = filePath.replace(/\\/g, '/');
    const rules: Record<string, string[]> = {
      Python: ['main-system/src-core/', 'shared-layer/src/', 'governance/', 'Standalone tools/'],
      JavaScript: ['main-system/src-ui/', 'main-system/scripts/'],
      TypeScript: ['main-system/src-ui/', 'main-system/scripts/'],
      Julia: ['Standalone tools/'],
      C: ['native/include/', 'native/bridge/', 'native/core/'],
      Cpp: ['native/core/', 'native/bridge/'],
      CSharp: [
        'main-system/launcher/src/',
        'Standalone tools/business-logic-csharp/',
        'Standalone tools/process-metrics-csharp/',
      ],
      FSharp: ['Standalone tools/'],
      Go: ['Standalone tools/'],
      Rust: ['Standalone tools/'],
      SQL: ['governance/', 'main-system/src-core/'],
    };

    const allowedDirs = rules[language] || [];
    const isAllowed = allowedDirs.some(dir => normalized.startsWith(dir));

    if (!isAllowed && !normalized.includes('node_modules') && !normalized.includes('.venv')) {
      return {
        rule: 'directory_valid',
        message: `${language} file in unauthorized directory: ${filePath} (A221/A215/A266)`,
        severity: 'error',
      };
    }
    return null;
  }

  checkProject(files: Map<string, string>): LanguageBoundaryResult[] {
    const results: LanguageBoundaryResult[] = [];
    for (const [file, content] of files) {
      results.push(this.checkFile(file, content));
    }
    return results;
  }
}

export function runLanguageBoundaryGate(
  files: Map<string, string>,
  grandfatheredPaths?: ReadonlySet<string>,
): {
  overall: LanguageVerdict;
  results: LanguageBoundaryResult[];
} {
  const checker = new LanguageBoundaryChecker({
    ...LANGUAGE_POLICY,
    grandfatheredPaths,
  });
  const results = checker.checkProject(files);
  const overall = results.some(r => r.verdict === 'FAIL') ? 'FAIL' :
                  results.some(r => r.verdict === 'WARN') ? 'WARN' : 'PASS';
  return { overall, results };
}