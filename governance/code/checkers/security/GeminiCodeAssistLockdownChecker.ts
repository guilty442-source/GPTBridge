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

const ruleFile = 'src-core/governance/rules/gemini_code_assist_lockdown.py'
const rulesEngineFile = 'src-core/governance/rules_engine.py'
const coreGovernanceFile = 'src-core/managers/core_governance.py'
const governanceDocFile = 'governance/docs/gemini-code-assist-lockdown-governance.md'

async function fileExists(relativePath: string): Promise<boolean> {
  try {
    await fs.access(path.resolve(process.cwd(), relativePath))
    return true
  } catch {
    return false
  }
}

async function readText(relativePath: string): Promise<string> {
  return fs.readFile(path.resolve(process.cwd(), relativePath), 'utf8')
}

function includesAll(content: string, requiredTexts: string[]): boolean {
  return requiredTexts.every((text) => content.includes(text))
}

export const geminiCodeAssistLockdownChecker: GovernanceChecker = {
  id: 'G-SEC-002',
  name: 'Gemini Code Assist Lockdown Checker',
  category: CheckerCategory.SECURITY,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-core/governance,src-core/managers,governance/docs',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '2026.05.30',
  run: async (): Promise<GovernanceReport> => {
    const missing: string[] = []

    const hasRuleFile = await fileExists(ruleFile)
    const hasRulesEngine = await fileExists(rulesEngineFile)
    const hasCoreGovernance = await fileExists(coreGovernanceFile)
    const hasDoc = await fileExists(governanceDocFile)

    if (!hasRuleFile) missing.push(ruleFile)
    if (!hasRulesEngine) missing.push(rulesEngineFile)
    if (!hasCoreGovernance) missing.push(coreGovernanceFile)
    if (!hasDoc) missing.push(governanceDocFile)

    if (missing.length > 0) {
      return {
        ruleId: 'G-SEC-002',
        passed: false,
        message:
          'Gemini Code Assist lockdown artifacts are missing. Governance check is blocking.',
        affectedFiles: missing,
        autofixAvailable: false,
      }
    }

    const rulesEngineContent = await readText(rulesEngineFile)
    const coreGovernanceContent = await readText(coreGovernanceFile)
    const docContent = await readText(governanceDocFile)

    const offenders: string[] = []

    const rulesEngineReady =
      includesAll(rulesEngineContent, [
        'from .rules.gemini_code_assist_lockdown import GeminiCodeAssistLockdownRule',
        'GeminiCodeAssistLockdownRule()',
      ])
    if (!rulesEngineReady) offenders.push(rulesEngineFile)

    const coreGovernanceReady =
      includesAll(coreGovernanceContent, [
        'GEMINI_CODE_ASSIST_DENIED_ALIASES',
        'is_write_denied_actor',
        'gemini_code_assist',
      ])
    if (!coreGovernanceReady) offenders.push(coreGovernanceFile)

    const docReady = includesAll(docContent, [
      'G-SEC-002',
      'gemini_code_assist_lockdown',
      'BLOCKING',
    ])
    if (!docReady) offenders.push(governanceDocFile)

    const passed = offenders.length === 0
    return {
      ruleId: 'G-SEC-002',
      passed,
      message: passed
        ? 'Gemini Code Assist lockdown governance is active and build-enforced.'
        : 'Gemini Code Assist lockdown governance is incomplete. Build must be blocked.',
      affectedFiles: Array.from(new Set(offenders)),
      autofixAvailable: false,
    }
  },
}
