import fs from 'node:fs'
import path from 'node:path'
import {
  CheckerCategory,
  CoverageStatus,
  EnforceLevel,
  type GovernanceChecker,
  type GovernanceReport,
  Severity,
} from '../../registry/GovernanceCheckerRegistry'
import { governancePaths } from '../../runtime/GovernancePaths'

const requiredFolders = [
  governancePaths.governanceExecutionRoot,
  governancePaths.resolveWithinProject(
    'governance_rule',
    'permission_directory',
    'execution'
  ),
]

const allowedRootDirectories = new Set([
  '.git', '.devin', '.smallcode', '.venv', '.vs', '.vscode',
  'docs', 'launcher', 'main-system', 'scripts', 'shared-layer',
])
const allowedRootFiles = new Set([
  '.env', '.gitignore', '.markdownlint.json', 'AGENTS.md', 'pytest.ini',
  '_check_backend.py', '_poll_health.py',
])

function hasMatchingManifest(directory: string, expectedId: string): boolean {
  const manifestPath = path.join(directory, 'manifest.json')
  if (!fs.existsSync(manifestPath)) return false
  try {
    const source = fs.readFileSync(manifestPath, 'utf8')
    const id = source.match(/"id"\s*:\s*"([^"]+)"/)?.[1]
    return id === expectedId
  } catch {
    return false
  }
}

function forbiddenLegacyPaths(): string[] {
  const authorityPath = governancePaths.resolveWithinProject(
    'governance_rule',
    'permission_directory',
    'directory_authority.py'
  )
  try {
    const source = fs.readFileSync(authorityPath, 'utf8')
    const block = source.match(
      /FORBIDDEN_LEGACY_ROOTS[^=]*=\s*\(([\s\S]*?)\r?\n\)/
    )?.[1]
    return block
      ? [...block.matchAll(/["']([^"']+)["']/g)].map((match) => match[1])
      : []
  } catch {
    return []
  }
}

export const rootPurityChecker: GovernanceChecker = {
  id: 'G-MODULE-001',
  name: 'Governance Root Layer Checker',
  category: CheckerCategory.MODULE,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'governance_rule',
  coverage: CoverageStatus.FULLY_ENFORCED,
  version: '2.0.0',
  run: async (): Promise<GovernanceReport> => {
    const missing = requiredFolders.filter(
      (folder) => !fs.existsSync(folder)
    )
    const unexpectedRootEntries = fs
      .readdirSync(governancePaths.projectRoot, { withFileTypes: true })
      .filter((entry) => {
        if (entry.isFile()) return !allowedRootFiles.has(entry.name)
        if (!entry.isDirectory()) return true
        if (allowedRootDirectories.has(entry.name)) return false
        return !hasMatchingManifest(
          path.join(governancePaths.projectRoot, entry.name),
          entry.name
        )
      })
      .map((entry) => governancePaths.resolveWithinProject(entry.name))
    const forbiddenLegacyEntries = forbiddenLegacyPaths()
      .map((relativePath) => governancePaths.resolveWithinProject(relativePath))
      .filter((candidate) => fs.existsSync(candidate))
    const violations = [
      ...missing,
      ...unexpectedRootEntries,
      ...forbiddenLegacyEntries,
    ].filter((candidate, index, all) => all.indexOf(candidate) === index)

    const passed = violations.length === 0
    return {
      ruleId: 'G-MODULE-001',
      passed,
      message: passed
        ? 'Project root contains only governed modules and approved control files.'
        : `Project root purity violations: ${violations.join(', ')}`,
      affectedFiles: violations,
      autofixAvailable: false,
    }
  },
}
