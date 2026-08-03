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
import { governancePaths, relativeToProject } from '../../runtime/GovernancePaths'
const scanRoot = governancePaths.mainRendererRoot
const localePath = path.join(governancePaths.mainSystemRoot, 'locales', 'zh-TW.json')

function collectLocaleStrings(value: unknown, output: Set<string>): void {
  if (typeof value === 'string') {
    if (value.trim().length >= 2) output.add(value)
    return
  }
  if (!value || typeof value !== 'object') return
  for (const child of Object.values(value)) collectLocaleStrings(child, output)
}

async function loadProtectedLabels(): Promise<string[]> {
  const payload = JSON.parse(await fs.readFile(localePath, 'utf8')) as unknown
  const labels = new Set<string>()
  collectLocaleStrings(payload, labels)
  return Array.from(labels)
}

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

    if (!entry.name.endsWith('.ts') && !entry.name.endsWith('.tsx')) {
      continue
    }

    output.push(fullPath)
  }

  return output
}

interface Offender {
  file: string
  label: string
}

export const localizationRenameChecker: GovernanceChecker = {
  id: 'G-I18N-001',
  name: 'Localization Rename Governance Checker',
  category: CheckerCategory.MODULE,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'main-system/src-ui/renderer',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '1.0.0',
  run: async (): Promise<GovernanceReport> => {
    const files = await collectSourceFiles(scanRoot)
    const protectedLabels = await loadProtectedLabels()
    const offenders: Offender[] = []

    for (const file of files) {
      const content = await fs.readFile(file, 'utf8')
      const relative = relativeToProject(file)

      for (const label of protectedLabels) {
        if (content.includes(label)) {
          offenders.push({ file: relative, label })
        }
      }
    }

    const uniqueFiles = Array.from(new Set(offenders.map((item) => item.file)))
    const passed = offenders.length === 0

    return {
      ruleId: 'G-I18N-001',
      passed,
      message: passed
        ? 'Rename-sensitive labels are controlled by locale files.'
        : `Found ${offenders.length} hardcoded rename-sensitive labels in ${uniqueFiles.length} files. Rename must be changed in main-system/locales/zh-TW.json only.`,
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
