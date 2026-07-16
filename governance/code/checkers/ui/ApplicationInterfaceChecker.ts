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
const applicationEntry = path.join(rendererRoot, 'ui', 'toolbox', 'ToolboxEntry.tsx')

export const applicationInterfaceChecker: GovernanceChecker = {
  id: 'G-UI-APP-001',
  name: 'Application Interface Naming Checker',
  category: CheckerCategory.UI,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-ui/renderer',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '1.0.0',
  run: async (): Promise<GovernanceReport> => {
    const affectedFiles: string[] = []
    const mainContent = await fs.readFile(mainEntry, 'utf8')
    if (!mainContent.includes("import App from './ui/App'")) {
      affectedFiles.push(path.relative(process.cwd(), mainEntry))
    }

    const appContent = await fs.readFile(appEntry, 'utf8')
    const importsBusinessUi =
      appContent.includes("@/ui/DeveloperMode") ||
      appContent.includes("@/ui/governance-rules") ||
      appContent.includes('@platform-ui/')
    const hasLegacyViews = appContent.includes('ViewMode') || appContent.includes('activeView')
    if (
      !appContent.includes('useToolboxApplications') ||
      !appContent.includes('<ToolboxEntry') ||
      importsBusinessUi ||
      hasLegacyViews
    ) {
      affectedFiles.push(path.relative(process.cwd(), appEntry))
    }

    const applicationContent = await fs.readFile(applicationEntry, 'utf8')
    if (
      !applicationContent.includes('RuntimeToolCard') ||
      !applicationContent.includes('toolbox-panel') ||
      !applicationContent.includes('toolbox-grid')
    ) {
      affectedFiles.push(path.relative(process.cwd(), applicationEntry))
    }

    const uniqueFiles = Array.from(new Set(affectedFiles)).sort()
    const passed = uniqueFiles.length === 0
    return {
      ruleId: 'G-UI-APP-001',
      passed,
      message: passed
        ? 'Application interface is governed: GPTBridge is launcher-only and independent applications render through RuntimeToolCard.'
        : 'Application interface drift detected. GPTBridge must stay launcher-only, render independent applications through RuntimeToolCard, and avoid business UI imports.',
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
