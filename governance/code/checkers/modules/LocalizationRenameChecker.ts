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
import { zhTW } from '../../../../src-ui/renderer/locales/zh-TW'

const scanRoot = path.resolve(process.cwd(), 'src-ui', 'renderer')
const excludedPrefixes = [
  path.join('src-ui', 'renderer', 'locales'),
  path.join('src-ui', 'renderer', 'i18n'),
  path.join('src-ui', 'renderer', 'shared', 'i18n'),
]

const protectedLabels = Array.from(
  new Set([
    zhTW.app.platformName,
    zhTW.mode.interfaceSystem,
    zhTW.mode.developer,
    zhTW.developer.title,
  ])
).filter((value): value is string => typeof value === 'string' && value.length > 0)

async function collectSourceFiles(root: string): Promise<string[]> {
  const output: string[] = []
  const entries = await fs.readdir(root, { withFileTypes: true })

  for (const entry of entries) {
    const fullPath = path.join(root, entry.name)
    const relative = path.relative(process.cwd(), fullPath)

    if (entry.isDirectory()) {
      if (
        entry.name === 'node_modules' ||
        entry.name === 'dist-ui' ||
        entry.name === 'release'
      ) {
        continue
      }
      if (excludedPrefixes.some((prefix) => relative.startsWith(prefix))) {
        continue
      }
      output.push(...(await collectSourceFiles(fullPath)))
      continue
    }

    if (!entry.name.endsWith('.ts') && !entry.name.endsWith('.tsx')) {
      continue
    }

    if (excludedPrefixes.some((prefix) => relative.startsWith(prefix))) {
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
  target: 'src-ui/renderer',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '2026.05.29',
  run: async (): Promise<GovernanceReport> => {
    const files = await collectSourceFiles(scanRoot)
    const offenders: Offender[] = []

    for (const file of files) {
      const content = await fs.readFile(file, 'utf8')
      const relative = path.relative(process.cwd(), file)

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
        : `Found ${offenders.length} hardcoded rename-sensitive labels in ${uniqueFiles.length} files. Rename must be changed in src-ui/renderer/locales/zh-TW.ts only.`,
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
