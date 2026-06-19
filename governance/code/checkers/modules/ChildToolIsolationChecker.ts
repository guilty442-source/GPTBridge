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
    return entries
      .filter((entry) => entry.isDirectory() && !entry.name.startsWith('_'))
      .map((entry) => entry.name)
      .sort()
  } catch {
    return []
  }
}

async function collectPlatformToolManifests(): Promise<string[]> {
  try {
    const entries = await fs.readdir(platformToolsRoot, { withFileTypes: true })
    return entries
      .filter((entry) => entry.isDirectory() && !entry.name.startsWith('_'))
      .map((entry) => path.join(platformToolsRoot, entry.name, 'manifest.json'))
      .sort()
  } catch {
    return []
  }
}

function includesChildToolHardcode(content: string, toolIds: string[]): boolean {
  return toolIds.some((toolId) => content.includes(toolId))
}

export const childToolIsolationChecker: GovernanceChecker = {
  id: 'G-115',
  name: 'Child Tool Isolation Checker',
  category: CheckerCategory.MODULE,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-core,src-ui/main,platform_tools',
  coverage: CoverageStatus.PARTIALLY_ENFORCED,
  version: '2026.06.19',
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
      }
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
