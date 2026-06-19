import { registry } from '../code/registry/GovernanceCheckerRegistry'
import { aliasConsistencyChecker } from '../code/checkers/import/AliasConsistencyChecker'
import { rootPurityChecker } from '../code/checkers/modules/RootPurityChecker'
import { localizationRenameChecker } from '../code/checkers/modules/LocalizationRenameChecker'
import { childToolIsolationChecker } from '../code/checkers/modules/ChildToolIsolationChecker'
import { pathLibraryChecker } from '../code/checkers/runtime/PathLibraryChecker'
import { singleEdgeBrowserChecker } from '../code/checkers/runtime/SingleEdgeBrowserChecker'
import { windows11BaselineChecker } from '../code/checkers/runtime/Windows11BaselineChecker'
import { geminiCodeAssistLockdownChecker } from '../code/checkers/security/GeminiCodeAssistLockdownChecker'

function registerBuiltInCheckers() {
  if (registry.getCheckers().length > 0) return
  registry.register(aliasConsistencyChecker)
  registry.register(rootPurityChecker)
  registry.register(localizationRenameChecker)
  registry.register(childToolIsolationChecker)
  registry.register(pathLibraryChecker)
  registry.register(singleEdgeBrowserChecker)
  registry.register(windows11BaselineChecker)
  registry.register(geminiCodeAssistLockdownChecker)
}

async function run() {
  registerBuiltInCheckers()
  const reports = await registry.runAll()
  let fixedCount = 0

  for (const report of reports) {
    if (report.passed) continue
    const checker = registry.getCheckers().find((item) => item.id === report.ruleId)
    if (checker?.autofix) {
      await checker.autofix()
      fixedCount += 1
      console.log(`[fixed] ${checker.id}`)
    }
  }

  if (fixedCount === 0) {
    console.log('[governance] No autofix actions were available.')
  }
}

void run()
