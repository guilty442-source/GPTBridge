import path from 'node:path'
import {
  CheckerCategory,
  registry,
  shouldBlockBuild,
} from '../code/registry/GovernanceCheckerRegistry'
import { aliasConsistencyChecker } from '../code/checkers/import/AliasConsistencyChecker'
import { globalUpdateCoordinatorChecker } from '../code/checkers/hmr/GlobalUpdateCoordinatorChecker'
import { rootPurityChecker } from '../code/checkers/modules/RootPurityChecker'
import { localizationRenameChecker } from '../code/checkers/modules/LocalizationRenameChecker'
import { childToolIsolationChecker } from '../code/checkers/modules/ChildToolIsolationChecker'
import { applicationInterfaceChecker } from '../code/checkers/ui/ApplicationInterfaceChecker'
import { governanceRulesInterfaceChecker } from '../code/checkers/ui/GovernanceRulesInterfaceChecker'
import { pathLibraryChecker } from '../code/checkers/runtime/PathLibraryChecker'
import { singleEdgeBrowserChecker } from '../code/checkers/runtime/SingleEdgeBrowserChecker'
import { windows11BaselineChecker } from '../code/checkers/runtime/Windows11BaselineChecker'
import { geminiCodeAssistLockdownChecker } from '../code/checkers/security/GeminiCodeAssistLockdownChecker'

function registerBuiltInCheckers() {
  if (registry.getCheckers().length > 0) return
  registry.register(aliasConsistencyChecker)
  registry.register(globalUpdateCoordinatorChecker)
  registry.register(rootPurityChecker)
  registry.register(localizationRenameChecker)
  registry.register(childToolIsolationChecker)
  registry.register(applicationInterfaceChecker)
  registry.register(governanceRulesInterfaceChecker)
  registry.register(pathLibraryChecker)
  registry.register(singleEdgeBrowserChecker)
  registry.register(windows11BaselineChecker)
  registry.register(geminiCodeAssistLockdownChecker)
}

function parseCategoryArg(): CheckerCategory | undefined {
  const args = process.argv.slice(2)
  const idx = args.indexOf('--category')
  if (idx === -1) return undefined
  const value = args[idx + 1]
  if (!value) return undefined
  if (Object.values(CheckerCategory).includes(value as CheckerCategory)) {
    return value as CheckerCategory
  }
  return undefined
}

async function run() {
  registerBuiltInCheckers()
  const category = parseCategoryArg()
  const reports = await registry.runAll(category)

  if (reports.length === 0) {
    console.log('[governance] No checkers registered.')
    return
  }

  for (const report of reports) {
    const label = report.passed ? 'PASS' : 'FAIL'
    console.log(`[${label}] ${report.ruleId}: ${report.message}`)
    if (!report.passed && report.affectedFiles?.length) {
      for (const file of report.affectedFiles) {
        console.log(`  - ${file}`)
      }
    }
  }

  if (shouldBlockBuild(reports)) {
    console.error(
      `[governance] Blocking failures detected in ${path.resolve(
        process.cwd(),
        'governance'
      )}.`
    )
    process.exitCode = 1
  }
}

void run()
