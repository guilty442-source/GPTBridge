#!/usr/bin/env node
/**
 * ts_to_esm.mjs -- governed TypeScript -> JavaScript-ESM migration tool.
 *
 * Contract: ts-to-esm/v1
 * Authority: A35/A348 (TypeScript retired -> JavaScript-ESM; grandfathered
 * files migrate atomically through this tool only).
 *
 * For each authored .ts/.tsx file the tool:
 *   1. type-strips via the vendored rolldown/oxc transformer (the same
 *      engine vite build uses -- no new dependency),
 *   2. rewrites relative import/export specifiers to the emitted extension
 *      (.ts -> .js, .tsx -> .jsx, directory -> /index.js[x]),
 *   3. maps CommonJS globals to native ESM (__dirname -> import.meta.dirname,
 *      __filename -> import.meta.filename),
 *   4. --apply performs an atomic per-file rename: write .js/.jsx then
 *      delete the .ts/.tsx source (git records the pair as a rename).
 *
 * Usage:
 *   node scripts/ts_to_esm.mjs --check  <files...>   # dry-run report (default)
 *   node scripts/ts_to_esm.mjs --apply  <files...>   # migrate in place
 *   node scripts/ts_to_esm.mjs --json   <files...>   # machine-readable report
 *
 * Fail-closed: refuses .d.ts inputs, non-existent files, transform errors,
 * and --apply when any file has unresolved relative specifiers or a
 * colliding target. Exit code: 0 clean / 1 refusal / 2 usage error.
 */

import { transform } from 'rolldown/experimental'
import { existsSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { resolve, dirname, extname, join } from 'node:path'

const CONTRACT = 'ts-to-esm/v1'

const args = process.argv.slice(2)
const apply = args.includes('--apply')
const mjsOut = args.includes('--mjs')
const jsonMode = args.includes('--json')
const files = args.filter((a) => !a.startsWith('--'))

const EMIT_EXT = { '.ts': '.js', '.tsx': '.jsx' }
const RESOLVE_ORDER = [
  ['.ts', '.js'],
  ['.tsx', '.jsx'],
  ['.js', '.js'],
  ['.jsx', '.jsx'],
  ['.mjs', '.mjs'],
  ['.cjs', '.cjs'],
  ['.json', '.json'],
]
const NON_CODE_EXT = [
  '.css', '.scss', '.sass', '.less', '.svg', '.png', '.jpg', '.jpeg',
  '.gif', '.webp', '.woff', '.woff2', '.ttf', '.json',
]

function usage() {
  console.error('usage: node scripts/ts_to_esm.mjs [--check|--apply] [--json] <files...>')
  process.exit(2)
}

function emitExt(sourcePath) {
  if (sourcePath.endsWith('.d.ts')) return null
  const e = EMIT_EXT[extname(sourcePath).toLowerCase()]
  return mjsOut && e === '.js' ? '.mjs' : e ?? null
}

/** Resolve a relative specifier to its emitted path; sentinel object when unresolved. */
function resolveSpecifier(spec, importerDir) {
  if (!spec.startsWith('./') && !spec.startsWith('../')) return undefined
  if (NON_CODE_EXT.some((e) => spec.endsWith(e))) return spec
  const base = resolve(importerDir, spec)
  if (extname(spec)) {
    const mapped = EMIT_EXT[extname(spec).toLowerCase()]
    if (mapped && existsSync(base)) return spec.slice(0, -extname(spec).length) + mapped
    if (existsSync(base)) return spec
    return { unresolved: spec }
  }
  for (const [srcExt, outExt] of RESOLVE_ORDER) {
    if (existsSync(base + srcExt)) return spec + outExt
  }
  for (const indexName of ['index.ts', 'index.tsx', 'index.js', 'index.jsx']) {
    if (existsSync(join(base, indexName))) {
      const out = EMIT_EXT[extname(indexName)] ?? extname(indexName)
      return spec + '/index' + out
    }
  }
  return { unresolved: spec }
}

const SPECIFIER_RE =
  /(\bfrom\s*|\bimport\s*\(|\bimport\s*|export\s+(?:\*|\{[^}]*\})\s+from\s*)(['"])(\.{1,2}\/[^'"]+)\2/g

async function migrateOne(sourcePath) {
  const abs = resolve(sourcePath)
  const targetExt = emitExt(abs)
  const report = {
    contract: CONTRACT,
    file: sourcePath,
    target: null,
    rewritten: [],
    unresolved: [],
    applied: false,
    error: null,
  }
  if (!targetExt) {
    report.error = abs.endsWith('.d.ts')
      ? 'DTS_DECLARATION_ONLY -- migrate/delete manually, nothing executable to emit'
      : `unsupported extension for ${sourcePath}`
    return report
  }
  if (!existsSync(abs)) {
    report.error = `file not found: ${abs}`
    return report
  }
  const target = abs.slice(0, -extname(abs).length) + targetExt
  report.target = target
  if (existsSync(target)) {
    report.error = `target already exists: ${target}`
    return report
  }

  const source = readFileSync(abs, 'utf8')
  const transpiled = await transform(abs, source, {
    jsx: targetExt === '.jsx' ? 'preserve' : undefined,
  })
  if (transpiled.errors?.length) {
    report.error = `transform errors: ${transpiled.errors.map((e) => e.message ?? e).join('; ')}`
    return report
  }

  const importerDir = dirname(abs)
  const rewritten = transpiled.code
    .replace(SPECIFIER_RE, (whole, prefix, quote, spec) => {
      const resolved = resolveSpecifier(spec, importerDir)
      if (resolved === undefined || typeof resolved === 'object') {
        if (resolved && resolved.unresolved) report.unresolved.push(spec)
        return whole
      }
      if (resolved !== spec) {
        report.rewritten.push({ from: spec, to: resolved })
        return `${prefix}${quote}${resolved}${quote}`
      }
      return whole
    })
    .replace(/\b__dirname\b/g, 'import.meta.dirname')
    .replace(/\b__filename\b/g, 'import.meta.filename')

  if (apply) {
    if (report.unresolved.length > 0) return report
    writeFileSync(target, rewritten, 'utf8')
    unlinkSync(abs)
    report.applied = true
  } else {
    report.previewBytes = rewritten.length
  }
  return report
}

if (files.length === 0) usage()
const reports = []
for (const f of files) reports.push(await migrateOne(f))
const failures = reports.filter((r) => r.error || (apply && r.unresolved.length))

if (jsonMode) {
  console.log(JSON.stringify({ contract: CONTRACT, applied: apply, reports }, null, 2))
} else {
  for (const r of reports) {
    if (r.error) {
      console.log(`REFUSE ${r.file}: ${r.error}`)
      continue
    }
    const state = r.applied ? 'MIGRATED' : 'PLAN'
    console.log(`${state} ${r.file} -> ${r.target}`)
    for (const w of r.rewritten) console.log(`  rewrite ${w.from} -> ${w.to}`)
    for (const u of r.unresolved) console.log(`  UNRESOLVED ${u}`)
  }
  console.log(
    `[${CONTRACT}] ${reports.length} file(s), ` +
      `${reports.reduce((n, r) => n + r.rewritten.length, 0)} specifier rewrites, ` +
      `${failures.length} refusal(s)`,
  )
}
if (failures.length > 0) process.exit(1)
