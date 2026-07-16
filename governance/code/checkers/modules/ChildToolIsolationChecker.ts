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

const scanRoots = [
  path.resolve(process.cwd(), 'src-core'),
  path.resolve(process.cwd(), 'src-ui', 'main'),
]
const platformToolsRoot = path.resolve(process.cwd(), 'platform_tools')

const sourceExtensions = new Set(['.py', '.ts', '.tsx', '.js', '.mjs', '.cjs'])
const skipDirs = new Set(['node_modules', 'dist-ui', 'release', '.venv', 'runtime', '__pycache__'])

async function collectSourceFiles(root: string): Promise<string[]> {
  const output: string[] = []
  const entries = await fs.readdir(root, { withFileTypes: true })

  for (const entry of entries) {
    const fullPath = path.join(root, entry.name)

    if (entry.isDirectory()) {
      if (skipDirs.has(entry.name)) {
        continue
      }
      output.push(...(await collectSourceFiles(fullPath)))
      continue
    }

    if (!sourceExtensions.has(path.extname(entry.name))) {
      continue
    }

    output.push(fullPath)
  }

  return output
}

async function collectPlatformToolIds(): Promise<string[]> {
  try {
    const entries = await fs.readdir(platformToolsRoot, { withFileTypes: true })
    const ids: string[] = []
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.name.startsWith('_')) continue
      try {
        await fs.access(path.join(platformToolsRoot, entry.name, 'manifest.json'))
        ids.push(entry.name)
      } catch {
        // A directory without a manifest is not a registered application.
      }
    }
    return ids.sort()
  } catch {
    return []
  }
}

async function collectPlatformToolManifests(): Promise<string[]> {
  return (await collectPlatformToolIds()).map((toolId) =>
    path.join(platformToolsRoot, toolId, 'manifest.json')
  )
}

function includesChildToolHardcode(content: string, toolIds: string[]): boolean {
  return toolIds.some((toolId) => content.includes(toolId))
}

function importsPlatformToolImplementation(content: string): boolean {
  return (
    /(?:^|\n)\s*(?:from|import)\s+platform_tools(?:\.|\s)/m.test(content) ||
    /(?:from|import)\s*['"][^'"]*platform_tools\//m.test(content)
  )
}

export const childToolIsolationChecker: GovernanceChecker = {
  id: 'G-115',
  name: 'Child Tool Isolation Checker',
  category: CheckerCategory.MODULE,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-core,src-ui/main,platform_tools',
  coverage: CoverageStatus.PARTIALLY_ENFORCED,
  version: '1.0.0',
  run: async (): Promise<GovernanceReport> => {
    const offenders: string[] = []
    const toolIds = await collectPlatformToolIds()
    const manifestPaths = await collectPlatformToolManifests()

    for (const toolId of toolIds) {
      const legacyCoreTask = path.resolve(process.cwd(), 'src-core', 'tasks', toolId)
      try {
        await fs.access(legacyCoreTask)
        offenders.push(path.relative(process.cwd(), legacyCoreTask))
      } catch {
        // Absence is the governed state.
      }
    }

    for (const manifestPath of manifestPaths) {
      try {
        const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8')) as {
          runtime?: { entry?: unknown }
          executable?: { path?: unknown }
        }
        const hasRuntimeEntry =
          typeof manifest.runtime?.entry === 'string' &&
          manifest.runtime.entry.trim().length > 0
        const hasExecutablePath =
          typeof manifest.executable?.path === 'string' &&
          manifest.executable.path.trim().length > 0
        if (!hasRuntimeEntry || !hasExecutablePath) {
          offenders.push(path.relative(process.cwd(), manifestPath))
        }
      } catch {
        offenders.push(path.relative(process.cwd(), manifestPath))
      }
    }

    const rendererEntry = path.resolve(process.cwd(), 'src-ui', 'renderer', 'main.tsx')
    try {
      const rendererEntryContent = await fs.readFile(rendererEntry, 'utf8')
      if (
        rendererEntryContent.includes('platform_tools/') ||
        rendererEntryContent.includes('toolWindowId')
      ) {
        offenders.push(path.relative(process.cwd(), rendererEntry))
      }
    } catch {
      offenders.push(path.relative(process.cwd(), rendererEntry))
    }

    for (const root of scanRoots) {
      const files = await collectSourceFiles(root)
      for (const file of files) {
        const content = await fs.readFile(file, 'utf8')
        if (includesChildToolHardcode(content, toolIds)) {
          offenders.push(path.relative(process.cwd(), file))
        }
        if (importsPlatformToolImplementation(content)) {
          offenders.push(path.relative(process.cwd(), file))
        }
      }
    }

    const runtimeContract = path.resolve(
      process.cwd(),
      'config',
      'tool-runtime-contract.json'
    )
    try {
      const contract = JSON.parse(await fs.readFile(runtimeContract, 'utf8')) as {
        contract_version?: unknown
        protocol_version?: unknown
        minimum_supported_contract_version?: unknown
      }
      if (
        !Number.isInteger(contract.contract_version) ||
        !Number.isInteger(contract.protocol_version) ||
        !Number.isInteger(contract.minimum_supported_contract_version) ||
        Number(contract.minimum_supported_contract_version) >
          Number(contract.contract_version)
      ) {
        offenders.push(path.relative(process.cwd(), runtimeContract))
      }
    } catch {
      offenders.push(path.relative(process.cwd(), runtimeContract))
    }

    const projectCleanerEngine = path.resolve(
      process.cwd(),
      'platform_tools',
      'project-cleaner',
      'src',
      'backend',
      'cleanup_engine.py'
    )
    const sharedCleanupPrimitive = path.resolve(
      process.cwd(),
      'src-core',
      'utils',
      'cleanup.py'
    )
    const projectEntry = path.resolve(process.cwd(), 'run.py')
    try {
      const [cleanerContent, primitiveContent, entryContent] =
        await Promise.all([
          fs.readFile(projectCleanerEngine, 'utf8'),
          fs.readFile(sharedCleanupPrimitive, 'utf8'),
          fs.readFile(projectEntry, 'utf8'),
        ])
      const cleanerBoundaryTokens = [
        'def repair_anomalies(',
        '"mutation_root"',
        '"outside-project"',
        '"source-code-edit"',
        '"force-unlock"',
        '"active-package-lock"',
        '"current-dist"',
        '"rollback-incomplete"',
      ]
      if (
        cleanerBoundaryTokens.some(
          (token) => !cleanerContent.includes(token)
        ) ||
        primitiveContent.includes('def perform_cleanup(') ||
        entryContent.includes('def run_recoverable_cleanup(') ||
        !entryContent.includes('def run_project_cleaner(') ||
        !entryContent.includes('"--auto-clean"')
      ) {
        offenders.push(path.relative(process.cwd(), projectCleanerEngine))
        offenders.push(path.relative(process.cwd(), sharedCleanupPrimitive))
        offenders.push(path.relative(process.cwd(), projectEntry))
      }
    } catch {
      offenders.push(path.relative(process.cwd(), projectCleanerEngine))
      offenders.push(path.relative(process.cwd(), sharedCleanupPrimitive))
      offenders.push(path.relative(process.cwd(), projectEntry))
    }

    const affectedFiles = Array.from(new Set(offenders)).sort()
    const passed = affectedFiles.length === 0

    return {
      ruleId: 'G-115',
      passed,
      message: passed
        ? 'Platform applications are standalone: each manifest declares an EXE and the mother renderer does not import tool UI.'
        : 'Found platform-tool hardcoding, missing standalone executable metadata, or legacy task folders. Keep application code in platform_tools/<tool-name>/ and launch it as its own EXE.',
      affectedFiles,
      autofixAvailable: false,
    }
  },
}
