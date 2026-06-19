import fs from 'node:fs/promises'
import path from 'node:path'
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

  const reportPayload = {
    generatedAt: new Date().toISOString(),
    summary: {
      total: reports.length,
      passed: reports.filter((report) => report.passed).length,
      failed: reports.filter((report) => !report.passed).length,
    },
    reports,
  }

  const reportDir = path.resolve(process.cwd(), 'governance', 'reports')
  await fs.mkdir(reportDir, { recursive: true })

  const stamp = new Date().toISOString().replace(/[:.]/g, '-')
  const reportPath = path.join(reportDir, `governance-report-${stamp}.json`)
  await fs.writeFile(reportPath, JSON.stringify(reportPayload, null, 2), 'utf8')
  console.log(`[governance] Report generated: ${reportPath}`)
}

void run()
