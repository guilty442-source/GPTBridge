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
const governanceRulesRoot = path.join(rendererRoot, 'ui', 'governance-rules')
const appEntry = path.join(rendererRoot, 'ui', 'App.tsx')
const localeEntry = path.join(rendererRoot, 'locales', 'zh-TW.ts')
const toolRuntimePanel = path.join(
  rendererRoot,
  'ui',
  'developer-mode',
  'tools',
  'ToolRuntimePanel.tsx'
)
const legacyGovernanceRulesCard = path.join(
  rendererRoot,
  'ui',
  'developer-mode',
  'governance',
  'GovernanceRulesCard.tsx'
)
const commandRouter = path.resolve(process.cwd(), 'src-core', 'ipc', 'handlers.py')
const ruleCatalog = path.resolve(
  process.cwd(),
  'src-core',
  'governance',
  'rule_catalog.py'
)

async function exists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath)
    return true
  } catch {
    return false
  }
}

async function readIfExists(filePath: string): Promise<string> {
  if (!(await exists(filePath))) return ''
  return fs.readFile(filePath, 'utf8')
}

export const governanceRulesInterfaceChecker: GovernanceChecker = {
  id: 'G-UI-GOV-001',
  name: 'Governance Rules Standalone Interface Checker',
  category: CheckerCategory.UI,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-ui/renderer/ui/governance-rules',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '2026.06.02',
  run: async (): Promise<GovernanceReport> => {
    const affectedFiles: string[] = []

    const requiredFiles = [
      'GovernanceRulesPage.tsx',
      'useGovernanceRules.ts',
      'ruleCode.ts',
      'types.ts',
      'index.ts',
      'governance-rules.css',
    ].map((file) => path.join(governanceRulesRoot, file))

    for (const file of requiredFiles) {
      if (!(await exists(file))) {
        affectedFiles.push(path.relative(process.cwd(), file))
      }
    }

    const appContent = await readIfExists(appEntry)
    if (
      !appContent.includes("@/ui/governance-rules") ||
      !appContent.includes("'governance'") ||
      !appContent.includes("activeView === 'governance'") ||
      !appContent.includes('zhTW.governanceRulesPage.title')
    ) {
      affectedFiles.push(path.relative(process.cwd(), appEntry))
    }

    const localeContent = await readIfExists(localeEntry)
    if (
      !localeContent.includes('governanceRulesPage') ||
      !localeContent.includes('治理規則管理') ||
      !localeContent.includes('一律啟動並全域生效') ||
      !localeContent.includes('自動轉換程式碼')
    ) {
      affectedFiles.push(path.relative(process.cwd(), localeEntry))
    }

    const panelContent = await readIfExists(toolRuntimePanel)
    if (
      panelContent.includes('GovernanceRulesCard') ||
      panelContent.includes('app:get-governance-rules')
    ) {
      affectedFiles.push(path.relative(process.cwd(), toolRuntimePanel))
    }

    if (await exists(legacyGovernanceRulesCard)) {
      affectedFiles.push(path.relative(process.cwd(), legacyGovernanceRulesCard))
    }

    const hookContent = await readIfExists(
      path.join(governanceRulesRoot, 'useGovernanceRules.ts')
    )
    if (
      !hookContent.includes('toGovernanceRuleCode') ||
      !hookContent.includes('app:update-governance-rule') ||
      !hookContent.includes('全部規則已全域啟動')
    ) {
      affectedFiles.push(
        path.relative(process.cwd(), path.join(governanceRulesRoot, 'useGovernanceRules.ts'))
      )
    }

    const routerContent = await readIfExists(commandRouter)
    if (
      !routerContent.includes('"app:update-governance-rule"') ||
      !routerContent.includes('"scope": "global"') ||
      !routerContent.includes('active_rules')
    ) {
      affectedFiles.push(path.relative(process.cwd(), commandRouter))
    }

    const catalogContent = await readIfExists(ruleCatalog)
    if (!catalogContent.includes('DEFAULT_ACTIVE_GOVERNANCE_RULES: Final[list[str]] = list(GOVERNANCE_RULE_CATALOG)')) {
      affectedFiles.push(path.relative(process.cwd(), ruleCatalog))
    }

    const uniqueFiles = Array.from(new Set(affectedFiles)).sort()
    const passed = uniqueFiles.length === 0

    return {
      ruleId: 'G-UI-GOV-001',
      passed,
      message: passed
        ? 'Governance rules are standalone, Chinese-visible, code-converting, and globally active.'
        : 'Governance rules drift detected. Keep rules in ui/governance-rules, expose Chinese UI, convert Chinese input to code, and enforce global activation.',
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
