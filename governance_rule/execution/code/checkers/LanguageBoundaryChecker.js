/**
 * LanguageBoundaryChecker.js — C55/D62 language boundary gate.
 *
 * READONLY-V1: scan + resolve + classify + compare + evidence +
 * PASS/WARN/FAIL only. This module performs no mutation: no import,
 * include, build, path or config rewrite; no file move; no exception or
 * allowlist creation (C55 FORBID).
 *
 * Input: a caller-supplied evidence set. Each entry is
 *   { path, content?, origin? }
 * where `content` is file text when cross-boundary scanning is required
 * and `origin` is one of LanguagePolicy.SOURCE_ORIGINS.
 * The checker never opens files itself; the caller owns IO, so results
 * are a pure function of the supplied evidence.
 *
 * Output: { verdict: PASS|WARN|FAIL, results: [...] } where every FAIL
 * carries source/target/edge/rule/reason/expected per the C55
 * diagnostic contract.
 */

import LanguagePolicy from '../policy/LanguagePolicy.js';

const {
  LANGUAGE_ROLES,
  CANONICAL_EXTENSIONS,
  GRANDFATHERED_EXTENSIONS,
  RETIRED_LANGUAGES,
  SOURCE_ROOTS,
  FORBIDDEN_CROSS_BOUNDARIES,
} = LanguagePolicy;

const normalize = (p) => String(p).replace(/\\/g, '/');

/** Longest-suffix-first canonical extension classification (A215:
 * `.d.ts` resolves as typescript before `.ts` ambiguity can bite). */
function classifyPath(path) {
  const lower = normalize(path).toLowerCase();
  const candidates = [];
  for (const [lang, exts] of Object.entries(CANONICAL_EXTENSIONS)) {
    for (const ext of exts) {
      if (lower.endsWith(ext)) candidates.push({ lang, ext });
    }
  }
  for (const [lang, exts] of Object.entries(GRANDFATHERED_EXTENSIONS)) {
    for (const ext of exts) {
      if (lower.endsWith(ext)) candidates.push({ lang, ext });
    }
  }
  candidates.sort((a, b) => b.ext.length - a.ext.length);
  return candidates.length ? candidates[0] : null;
}

function extensionOf(path) {
  const lower = normalize(path).toLowerCase();
  const all = [
    ...Object.values(CANONICAL_EXTENSIONS).flat(),
    ...Object.values(GRANDFATHERED_EXTENSIONS).flat(),
  ];
  const hit = all.filter((e) => lower.endsWith(e))
    .sort((a, b) => b.length - a.length)[0];
  return hit ?? null;
}

function violation({ source, target = 'none', edge = 'none->none', rule,
                     reason, expected }) {
  return { source, target, edge, rule, reason, expected };
}

// Cross-boundary import evidence patterns. Kept tight: only genuine
// binding/import sites, never comment or identifier text (the retired
// TypeScript lane's regex scanner produced false positives on words
// like `document.get` inside dict keys — D64: plain-text matching
// cannot establish a verdict, so these patterns only produce evidence
// rows for the host resolver, never a verdict by themselves).
const FORBIDDEN_PATTERNS = Object.freeze({
  database: [
    /^\s*import\s+.*['"](?:sqlite|postgres|pg|sql)/,
    /^\s*from\s+['"](?:sqlite|postgres|pg|sql)/,
  ],
  python: [/^\s*import\s+.*['"][^'"]*\.py['"]/],
  typescript: [/^\s*import\s+.*['"][^'"]*\.tsx?['"]/],
  julia: [/^\s*import\s+.*['"][^'"]*\.jl['"]/],
  native: [
    /^\s*import\s+.*['"](?:node:)?(?:ffi|addon|native)/,
    /\bcreateRequire\b.*['"][^'"]*\.(?:so|dll|node)['"]/,
  ],
  'process-spawn': [
    /^\s*import\s+.*['"](?:node:)?child_process['"]/,
    /\bfrom\s+['"](?:node:)?child_process['"]/,
  ],
  'worker-dom-storage': [
    /\bnew\s+Worker\s*\(/,
    /\blocalStorage\b/, /\bsessionStorage\b/, /\bindexedDB\b/,
  ],
  'filesystem-bridge': [
    /^\s*import\s+.*['"](?:node:)?fs['"]/,
  ],
  sql: [
    /^\s*import\s+.*['"](?:sqlite|postgres|pg)['"]/,
    /\.(?:execute|query)\s*\(\s*['"`]\s*(?:SELECT|INSERT|UPDATE|DELETE)\b/i,
  ],
  ui: [
    /^\s*import\s+.*['"](?:react|vue|svelte|preact)/,
    /\bdocument\.(?:querySelector|getElementById|createElement)\b/,
  ],
  domain: [/^\s*import\s+.*['"][^'"]*domain[^'"]*['"]/],
  permission: [/\bpermission[-_]sovereign\b/],
  governance: [/^\s*import\s+.*['"][^'"]*governance[^'"]*['"]/],
  'private-bridge': [/^\s*import\s+.*['"][^'"]*private[-_]bridge[^'"]*['"]/],
});

function scanLine(line, language) {
  // Only genuine import/require/binding statements produce evidence —
  // never bare identifier text.
  if (!/^\s*(import|export\s+.*from|const\s+\w+\s*=\s*require|require\()/.test(line)
      && !/\bnew\s+Worker\s*\(|\b(?:local|session)Storage\b|\bindexedDB\b|\bcreateRequire\b|\bdocument\.(?:querySelector|getElementById|createElement)\b/.test(line)) {
    return [];
  }
  const hits = [];
  for (const edge of FORBIDDEN_CROSS_BOUNDARIES[language] ?? []) {
    for (const pat of FORBIDDEN_PATTERNS[edge] ?? []) {
      if (pat.test(line)) hits.push(edge);
    }
  }
  return hits;
}

/** Check one evidence entry. */
export function checkFile(entry) {
  const path = normalize(entry.path);
  const language = classifyPath(path)?.lang ?? null;

  if (!language) {
    return {
      verdict: 'FAIL',
      file: path,
      language: 'unknown',
      violations: [violation({
        source: path,
        rule: 'D62:language_allowed',
        reason: 'unclassified-source-language',
        expected: 'a path under SOURCE_ROOTS with a canonical extension',
      })],
    };
  }

  const violations = [];
  const warnings = [];

  // Retired language: authored source is denied outright.
  if (RETIRED_LANGUAGES.includes(language)) {
    const retired = entry.origin !== 'THIRD_PARTY';
    if (retired) {
      violations.push(violation({
        source: path,
        edge: `${language}->authored-source`,
        rule: 'B166:retired-language-authored-source',
        reason: `${language} is retired; no authored-source role remains`,
        expected: 'no new-authored source in a retired language',
      }));
    }
  } else if (!(language in LANGUAGE_ROLES)) {
    violations.push(violation({
      source: path,
      rule: 'D62:language_allowed',
      reason: `language ${language} has no registered role`,
      expected: 'a language in LANGUAGE_ROLES',
    }));
  }

  // Extension validity.
  const ext = extensionOf(path);
  const canonical = CANONICAL_EXTENSIONS[language] ?? [];
  const grandfathered = GRANDFATHERED_EXTENSIONS[language] ?? [];
  if (ext && !canonical.includes(ext)) {
    if (grandfathered.includes(ext)) {
      warnings.push(`grandfathered extension ${ext} on ${path}`);
    } else {
      violations.push(violation({
        source: path,
        rule: 'D62:extension_valid',
        reason: `extension ${ext} is not canonical for ${language}`,
        expected: `one of ${canonical.join('|')}`,
      }));
    }
  }

  // Directory validity.
  const roots = SOURCE_ROOTS[language] ?? [];
  if (!roots.some((root) => path.startsWith(root) || path.includes('/' + root))) {
    violations.push(violation({
      source: path,
      rule: 'D62:directory_valid',
      reason: `${language} source outside the registered directory allowlist`,
      expected: `one of ${roots.join('|')}`,
    }));
  }

  // Cross-boundary scan — evidence rows only; never a verdict alone.
  if (typeof entry.content === 'string') {
    const seen = new Set();
    entry.content.split('\n').forEach((line, i) => {
      for (const edge of scanLine(line.trim(), language)) {
        if (seen.has(edge)) continue;
        seen.add(edge);
        violations.push(violation({
          source: path,
          target: edge,
          edge: `${language}->${edge}`,
          rule: 'A352:forbidden-cross-boundary',
          reason: `line ${i + 1}: ${language} edge to ${edge} is denied`,
          expected: `no ${language}->${edge} edge`,
        }));
      }
    });
  }

  const verdict = violations.length ? 'FAIL' : warnings.length ? 'WARN' : 'PASS';
  return { verdict, file: path, language, violations, warnings };
}

/** Check a whole evidence set. `entries` is an iterable of
 * { path, content?, origin? }. */
export function checkProject(entries) {
  const results = [];
  for (const entry of entries) results.push(checkFile(entry));
  const overall = results.some((r) => r.verdict === 'FAIL') ? 'FAIL'
    : results.some((r) => r.verdict === 'WARN') ? 'WARN' : 'PASS';
  return { overall, results };
}

export const CHECKER_ID = 'language-boundary-checker';
export const CHECKER_VERSION = '1.0.0';
export const RULES = Object.freeze(['D62', 'B166', 'A352', 'C55']);
