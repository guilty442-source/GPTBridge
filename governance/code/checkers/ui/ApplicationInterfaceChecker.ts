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

const rendererRoot = path.resolve(process.cwd(), 'src-ui', 'renderer')
const mainEntry = path.join(rendererRoot, 'main.tsx')
const appEntry = path.join(rendererRoot, 'ui', 'App.tsx')
const localeEntry = path.join(rendererRoot, 'locales', 'zh-TW.ts')
const applicationEntry = path.join(rendererRoot, 'ui', 'toolbox', 'ToolboxEntry.tsx')
const sourceExtensions = new Set(['.ts', '.tsx'])

async function collectSourceFiles(root: string): Promise<string[]> {
  const output: string[] = []
  const entries = await fs.readdir(root, { withFileTypes: true })

  for (const entry of entries) {
    const fullPath = path.join(root, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === 'dist-ui') continue
      output.push(...(await collectSourceFiles(fullPath)))
      continue
    }
    if (sourceExtensions.has(path.extname(entry.name))) {
      output.push(fullPath)
    }
  }

  return output
}

export const applicationInterfaceChecker: GovernanceChecker = {
  id: 'G-UI-APP-001',
  name: 'Application Interface Naming Checker',
  category: CheckerCategory.UI,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-ui/renderer',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '2026.06.02',
  run: async (): Promise<GovernanceReport> => {
    const affectedFiles: string[] = []

    const mainContent = await fs.readFile(mainEntry, 'utf8')
    if (!mainContent.includes("import App from './ui/App'")) {
      affectedFiles.push(path.relative(process.cwd(), mainEntry))
    }

    const appContent = await fs.readFile(appEntry, 'utf8')
    const importsStandaloneToolUi =
      appContent.includes("@/ui/DeveloperMode") ||
      appContent.includes("@/ui/governance-rules") ||
      appContent.includes('@platform-ui/ai-assistant')
    const keepsLegacyViewSwitch =
      appContent.includes('ViewMode') || appContent.includes('activeView')
    if (
      !appContent.includes('useToolboxApplications') ||
      !appContent.includes('<ToolboxEntry') ||
      !appContent.includes('應用程式啟動器') ||
      importsStandaloneToolUi ||
      keepsLegacyViewSwitch
    ) {
      affectedFiles.push(path.relative(process.cwd(), appEntry))
    }

    const localeContent = await fs.readFile(localeEntry, 'utf8')
    if (!localeContent.includes("title: '應用程式'")) {
      affectedFiles.push(path.relative(process.cwd(), localeEntry))
    }

    const applicationContent = await fs.readFile(applicationEntry, 'utf8')
    if (
      !applicationContent.includes('RuntimeToolCard') ||
      !applicationContent.includes('devm-tool-panel') ||
      !applicationContent.includes('devm-tool-grid')
    ) {
      affectedFiles.push(path.relative(process.cwd(), applicationEntry))
    }

    const files = await collectSourceFiles(rendererRoot)
    for (const file of files) {
      const content = await fs.readFile(file, 'utf8')
      if (content.includes('工具箱')) {
        affectedFiles.push(path.relative(process.cwd(), file))
      }
    }

    const uniqueFiles = Array.from(new Set(affectedFiles)).sort()
    const passed = uniqueFiles.length === 0

    return {
      ruleId: 'G-UI-APP-001',
      passed,
      message: passed
        ? 'Application interface is governed: GPTBridge is launcher-only, UI shows 應用程式, and standalone applications render through RuntimeToolCard.'
        : 'Application interface drift detected. GPTBridge must stay launcher-only, user-facing UI must use 應用程式, render through RuntimeToolCard, and avoid 工具箱 wording; internal toolbox IPC names may remain technical.',
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
