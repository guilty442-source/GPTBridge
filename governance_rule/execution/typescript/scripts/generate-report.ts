import { registry } from '../code/registry/GovernanceCheckerRegistry'
import { aliasConsistencyChecker } from '../code/checkers/import/AliasConsistencyChecker'
import { rootPurityChecker } from '../code/checkers/modules/RootPurityChecker'
import { localizationRenameChecker } from '../code/checkers/modules/LocalizationRenameChecker'
import { childToolIsolationChecker } from '../code/checkers/modules/ChildToolIsolationChecker'
import { pathLibraryChecker } from '../code/checkers/runtime/PathLibraryChecker'

function registerBuiltInCheckers() {
  if (registry.getCheckers().length > 0) return
  registry.register(aliasConsistencyChecker)
  registry.register(rootPurityChecker)
  registry.register(localizationRenameChecker)
  registry.register(childToolIsolationChecker)
  registry.register(pathLibraryChecker)
}

async function run() {
  registerBuiltInCheckers()
  const reports = await registry.runAll()

  const reportPayload = {
    generatedAt: new Date().toISOString(),
    summary: {
      total: reports.length,
      passed: reports.filter((report) => report.passed).length,
      failed: reports.filter((report) => !report.passed).length,
    },
    reports,
  }

  // Governance execution has no storage writer capability. The owning
  // System Rescue tool may persist this stdout payload after authorization.
  console.log(JSON.stringify(reportPayload))
}

void run()
