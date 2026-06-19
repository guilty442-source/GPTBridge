import fs from 'node:fs/promises'
import path from 'node:path'
import {
  CheckerCategory,
  CoverageStatus,
  EnforceLevel,
  type GovernanceChecker,
  type GovernanceReport,
  Severity,
} from '../../registry/GovernanceCheckerRegistry'

const importPattern = /from\s+['"]([^'"]+)['"]/g

async function collectSourceFiles(root: string): Promise<string[]> {
  const output: string[] = []
  const entries = await fs.readdir(root, { withFileTypes: true })

  for (const entry of entries) {
    const fullPath = path.join(root, entry.name)
    if (entry.isDirectory()) {
      if (
        entry.name === 'node_modules' ||
        entry.name === 'dist-ui' ||
        entry.name === 'release'
      ) {
        continue
      }
      output.push(...(await collectSourceFiles(fullPath)))
      continue
    }

    if (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) {
      output.push(fullPath)
    }
  }

  return output
}

async function findDeepRelativeImports(file: string): Promise<boolean> {
  const content = await fs.readFile(file, 'utf8')
  let match: RegExpExecArray | null
  while ((match = importPattern.exec(content))) {
    const source = match[1]
    if (source.startsWith('../../../') || source.startsWith('../../../../')) {
      return true
    }
  }
  return false
}

export const aliasConsistencyChecker: GovernanceChecker = {
  id: 'G-ALIAS-001',
  name: 'Alias Consistency Checker',
  category: CheckerCategory.IMPORT,
  severity: Severity.WARNING,
  enforceLevel: EnforceLevel.WARNING,
  target: 'src-ui/renderer',
  coverage: CoverageStatus.PARTIALLY_ENFORCED,
  version: '2026.05.29',
  run: async (): Promise<GovernanceReport> => {
    const root = path.resolve(process.cwd(), 'src-ui', 'renderer')
    const files = await collectSourceFiles(root)
    const offenders: string[] = []

    for (const file of files) {
      if (await findDeepRelativeImports(file)) {
        offenders.push(path.relative(process.cwd(), file))
      }
    }

    const passed = offenders.length === 0
    return {
      ruleId: 'G-ALIAS-001',
      passed,
      message: passed
        ? 'Alias consistency check passed.'
        : `Found ${offenders.length} files using deep relative imports.`,
      affectedFiles: offenders,
      autofixAvailable: false,
    }
  },
}
