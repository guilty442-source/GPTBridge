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

const requiredFolders = [
  'governance/code',
  'governance/docs',
  'governance/scripts',
  'governance/reports',
]

export const rootPurityChecker: GovernanceChecker = {
  id: 'G-MODULE-001',
  name: 'Governance Root Layer Checker',
  category: CheckerCategory.MODULE,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'governance',
  coverage: CoverageStatus.FULLY_ENFORCED,
  version: '2026.05.29',
  run: async (): Promise<GovernanceReport> => {
    const missing = requiredFolders.filter(
      (folder) => !fs.existsSync(path.resolve(process.cwd(), folder))
    )

    const passed = missing.length === 0
    return {
      ruleId: 'G-MODULE-001',
      passed,
      message: passed
        ? 'Governance layered folders are present.'
        : `Missing governance folders: ${missing.join(', ')}`,
      affectedFiles: missing,
      autofixAvailable: false,
    }
  },
}
