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
import {
  governancePaths,
  independentToolRoot,
  relativeToProject,
} from '../../runtime/GovernancePaths'

const scanRoots = [
  governancePaths.mainCoreRoot,
  governancePaths.mainProcessRoot,
  governancePaths.mainRendererRoot,
]
const projectRoot = governancePaths.projectRoot

const sourceExtensions = new Set(['.py', '.ts', '.tsx', '.js', '.mjs', '.cjs'])
const skipDirs = new Set(['node_modules', 'dist-ui', 'release', '.venv', 'runtime', '__pycache__'])
const forbiddenMainBusinessTerms = [
  'ai_nexus',
  'chatgpt',
  'claude',
  'deepseek',
  'dividend',
  'gemini',
  'grok',
  'holdings',
  'investment_mobile',
  'xingcheng',
  'perplexity',
  'portfolio',
]

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

async function collectIndependentToolIds(): Promise<string[]> {
  try {
    const entries = await fs.readdir(projectRoot, { withFileTypes: true })
    const ids: string[] = []
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.name.startsWith('_')) continue
      try {
        await fs.access(path.join(projectRoot, entry.name, 'manifest.json'))
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

async function collectIndependentToolManifests(): Promise<string[]> {
  return (await collectIndependentToolIds()).map((toolId) =>
    path.join(independentToolRoot(toolId), 'manifest.json')
  )
}

function importsPlatformToolImplementation(content: string): boolean {
  return (
    /(?:^|\n)\s*(?:from|import)\s+platform_tools(?:\.|\s)/m.test(content) ||
    /(?:from|import)\s*['"][^'"]*platform_tools\//m.test(content)
  )
}

function containsToolBusinessKnowledge(content: string): boolean {
  const normalized = content.toLocaleLowerCase('en-US')
  return forbiddenMainBusinessTerms.some((term) => normalized.includes(term))
}

export const childToolIsolationChecker: GovernanceChecker = {
  id: 'G-115',
  name: 'Child Tool Isolation Checker',
  category: CheckerCategory.MODULE,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'main-system,independent-tool-direct-roots',
  coverage: CoverageStatus.FULLY_ENFORCED,
  version: '1.0.0',
  run: async (): Promise<GovernanceReport> => {
    const offenders: string[] = []
    const toolIds = await collectIndependentToolIds()
    const manifestPaths = await collectIndependentToolManifests()

    for (const toolId of toolIds) {
      const legacyCoreTask = path.resolve(
        governancePaths.mainCoreRoot,
        'tasks',
        toolId
      )
      try {
        await fs.access(legacyCoreTask)
        offenders.push(relativeToProject(legacyCoreTask))
      } catch {
        // Absence is the governed state.
      }
    }

    for (const manifestPath of manifestPaths) {
      try {
        const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8')) as {
          id?: unknown
          runtime?: { entry?: unknown }
          executable?: { path?: unknown }
          distribution?: { mode?: unknown; package?: unknown }
          request_channel?: {
            model?: unknown
            runtime_entry?: unknown
            direct_instruction?: unknown
          }
          lifecycle?: {
            startup?: unknown
            directLoad?: unknown
            encapsulated?: unknown
            optional?: unknown
            stoppable?: unknown
          }
        }
        const hasRuntimeEntry =
          typeof manifest.runtime?.entry === 'string' &&
          manifest.runtime.entry.trim().length > 0
        const hasExecutablePath =
          typeof manifest.executable?.path === 'string' &&
          manifest.executable.path.trim().length > 0
        const isGovernanceAuthority = manifest.id === 'governance_rule'
        const isGovernedSourceRuntime =
          !hasExecutablePath &&
          manifest.distribution?.mode === 'special-unpackaged' &&
          manifest.distribution?.package === false &&
          manifest.request_channel?.model ===
            'governance-authenticated-shared-layer' &&
          typeof manifest.request_channel?.runtime_entry === 'string' &&
          manifest.request_channel.runtime_entry.trim().length > 0 &&
          manifest.request_channel?.direct_instruction === 'PERMISSION_DENIED'
        const governanceLifecycleValid =
          isGovernanceAuthority &&
          !hasExecutablePath &&
          manifest.lifecycle?.startup === 'default-before-main-system' &&
          manifest.lifecycle?.directLoad === true &&
          manifest.lifecycle?.encapsulated === false &&
          manifest.lifecycle?.optional === false &&
          manifest.lifecycle?.stoppable === false
        if (
          !hasRuntimeEntry ||
          (isGovernanceAuthority
            ? !governanceLifecycleValid
            : !hasExecutablePath && !isGovernedSourceRuntime)
        ) {
          offenders.push(relativeToProject(manifestPath))
        }
      } catch {
        offenders.push(relativeToProject(manifestPath))
      }
    }

    const rendererEntry = path.resolve(
      governancePaths.mainRendererRoot,
      'main.tsx'
    )
    try {
      const rendererEntryContent = await fs.readFile(rendererEntry, 'utf8')
      if (
        rendererEntryContent.includes('platform_tools/') ||
        rendererEntryContent.includes('toolWindowId')
      ) {
        offenders.push(relativeToProject(rendererEntry))
      }
    } catch {
      offenders.push(relativeToProject(rendererEntry))
    }

    for (const root of scanRoots) {
      const files = await collectSourceFiles(root)
      for (const file of files) {
        const content = await fs.readFile(file, 'utf8')
        if (
          importsPlatformToolImplementation(content) ||
          containsToolBusinessKnowledge(content)
        ) {
          offenders.push(relativeToProject(file))
        }
      }
    }

    const legacyMainPackager = path.resolve(
      governancePaths.mainSystemRoot,
      'scripts',
      'package_platform_tools.py'
    )
    try {
      await fs.access(legacyMainPackager)
      offenders.push(relativeToProject(legacyMainPackager))
    } catch {
      // No legacy main-system packager present.
    }

    const runtimeContract = path.resolve(
      governancePaths.mainConfigRoot,
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
        offenders.push(relativeToProject(runtimeContract))
      }
    } catch {
      offenders.push(relativeToProject(runtimeContract))
    }

    const affectedFiles = Array.from(new Set(offenders)).sort()
    const passed = affectedFiles.length === 0

    return {
      ruleId: 'G-115',
      passed,
      message: passed
        ? 'Independent applications are direct GPTBridge children; main-system source contains no tool business implementation, and System Rescue owns packaging.'
        : 'Found tool business knowledge, packaging ownership drift, a cross-tool implementation import, or invalid lifecycle metadata.',
      affectedFiles,
      autofixAvailable: false,
    }
  },
}
