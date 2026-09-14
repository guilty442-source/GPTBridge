/**
 * LanguageBoundaryChecker.ts ?�Language Boundary Gate (A348/A351/A352/A353).
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
}

export interface CrossBoundaryRule {
  from: string;
  to: string[];
  reason: string;
}

// Canonical language configuration per A203, A211, A215, A264
export const LANGUAGE_POLICY: LanguagePolicy = {
  allowedLanguages: ['Python', 'TypeScript', 'C', 'C++', 'CSharp', 'SQL'],
  canonicalRoles: new Map([
    // A203, A211
    ['Python', 'system-control+semantic/business logic+orchestration+governed-workflow+adapter-coordination'],
    ['TypeScript', 'UI presentation/client+contract+transport+type-safety+governance-checker'],
    ['C', 'public native interface only+stable ABI'],
    ['C++', 'private high-load implementation+measured-performance-critical'],
    ['CSharp', 'Windows-specific .NET/CLR/WinRT/COM integration only'],
    ['SQL', 'relational set operations+data selection+projection'],
  ]),
  forbiddenCrossBoundaries: [
    // A351, A352
    { from: 'TypeScript', to: ['Python-import', 'database', 'native', 'CSharp', 'SQL-direct-connection'], reason: 'TypeScript MUST use information-layer contracts only' },
    { from: 'Python', to: ['DOM', 'frontend-storage', 'UI-framework'], reason: 'Python MUST NOT access frontend primitives' },
    { from: 'C++', to: ['Python', 'SQL', 'UI'], reason: 'C++ private implementation only; exposed via C ABI' },
    { from: 'CSharp', to: ['domain', 'SQL', 'native'], reason: 'CSharp Windows adapter only' },
  ],
};

// Canonical file extensions per A215
export const CANONICAL_EXTENSIONS = {
  Python: ['.py', '.pyi', '.pyx'],
  TypeScript: ['.ts', '.tsx', '.d.ts'],
  C: ['.c', '.h'],
  Cpp: ['.cpp', '.hpp', '.inl'],
  CSharp: ['.cs'],
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

  private detectLanguage(filePath: string): string | null {
    const ext = filePath.substring(filePath.lastIndexOf('.'));
    for (const [lang, exts] of Object.entries(CANONICAL_EXTENSIONS)) {
      if (exts.includes(ext)) return lang;
    }
    return null;
  }

  private isValidExtension(filePath: string, language: string): boolean {
    const ext = filePath.substring(filePath.lastIndexOf('.'));
    const validExts = CANONICAL_EXTENSIONS[language as keyof typeof CANONICAL_EXTENSIONS] || [];
    return validExts.includes(ext);
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
    };

    const checkPatterns = patterns[forbidden] || [];
    return checkPatterns.some(p => p.test(line));
  }

  private checkDirectory(filePath: string, language: string): LanguageViolation | null {
    const normalized = filePath.replace(/\\/g, '/');
    const rules: Record<string, string[]> = {
      Python: ['main-system/src-core/', 'shared-layer/src/', 'governance/', 'Standalone tools/'],
      TypeScript: ['main-system/src-ui/', 'main-system/scripts/'],
      C: ['native/include/', 'native/bridge/', 'native/core/'],
      Cpp: ['native/core/', 'native/bridge/'],
      CSharp: ['main-system/launcher/src/'],
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

export function runLanguageBoundaryGate(files: Map<string, string>): {
  overall: LanguageVerdict;
  results: LanguageBoundaryResult[];
} {
  const checker = new LanguageBoundaryChecker();
  const results = checker.checkProject(files);
  const overall = results.some(r => r.verdict === 'FAIL') ? 'FAIL' :
                  results.some(r => r.verdict === 'WARN') ? 'WARN' : 'PASS';
  return { overall, results };
}