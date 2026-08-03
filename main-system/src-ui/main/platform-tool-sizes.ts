import fs from 'node:fs'
import path from 'node:path'

type PlatformToolSize = {
  id: string
  folder_path: string
  manifest_path: string
  code_path: string
  project_size_bytes: number
  file_count: number
  size_breakdown: ToolSizeBreakdown
}

type ToolSizeCategory =
  | 'program'
  | 'runtime'
  | 'user_data'
  | 'cache'
  | 'backups'

type ToolCategorySize = {
  size_bytes: number
  file_count: number
}

export type ToolSizeBreakdown = Record<ToolSizeCategory, ToolCategorySize>

type CachedInventory = {
  workspaceRoot: string
  expiresAt: number
  tools: PlatformToolSize[]
}

type FolderInventorySize = {
  folder_path: string
  project_size_bytes: number
  file_count: number
}

export type MainSystemSize = FolderInventorySize & {
  dependency_size_bytes: number
  dependency_file_count: number
  total_size_bytes: number
  total_file_count: number
}

export type WorkspaceSize = FolderInventorySize

export type SharedLayerSize = FolderInventorySize

type CachedMainSystemSize = MainSystemSize & {
  workspaceRoot: string
  expiresAt: number
}

type CachedFolderInventorySize = FolderInventorySize & {
  workspaceRoot: string
  expiresAt: number
}

const CACHE_TTL_MS = 30_000
const YIELD_EVERY_ENTRIES = 256
const MAIN_SYSTEM_DEPENDENCY_DIRECTORIES = new Set(['.venv', 'node_modules'])
const TOOL_RUNTIME_ROOTS = new Set([
  '.venv',
  'build',
  'dist',
  'env',
  'node_modules',
  'release',
  'venv',
])
const TOOL_CACHE_SEGMENTS = new Set([
  '.cache',
  '.pytest_cache',
  '.ruff_cache',
  '__pycache__',
  'browser-profile',
  'browser-profiles',
  'cache',
  'caches',
  'code cache',
  'edge-profile',
  'electron-user-data',
  'gpu cache',
  'temp',
  'tmp',
])
const TOOL_USER_DATA_RUNTIME_ROOTS = new Set([
  'data',
  'recovery',
  'settings',
  'state',
])
let cachedInventory: CachedInventory | null = null
let inventoryPromise: Promise<PlatformToolSize[]> | null = null
let cachedMainSystemSize: CachedMainSystemSize | null = null
let mainSystemSizePromise: Promise<MainSystemSize> | null = null
let cachedWorkspaceSize: CachedFolderInventorySize | null = null
let workspaceSizePromise: Promise<WorkspaceSize> | null = null
let cachedSharedLayerSize: CachedFolderInventorySize | null = null
let sharedLayerSizePromise: Promise<SharedLayerSize> | null = null

function isPathInside(basePath: string, targetPath: string): boolean {
  const relative = path.relative(basePath, targetPath)
  return (
    relative === '' ||
    (!!relative && !relative.startsWith('..') && !path.isAbsolute(relative))
  )
}

async function yieldToEventLoop(): Promise<void> {
  await new Promise<void>((resolve) => setImmediate(resolve))
}

function emptyToolSizeBreakdown(): ToolSizeBreakdown {
  return {
    program: { size_bytes: 0, file_count: 0 },
    runtime: { size_bytes: 0, file_count: 0 },
    user_data: { size_bytes: 0, file_count: 0 },
    cache: { size_bytes: 0, file_count: 0 },
    backups: { size_bytes: 0, file_count: 0 },
  }
}

export function classifyToolFile(relativePath: string): ToolSizeCategory {
  const segments = relativePath
    .split(/[\\/]+/)
    .filter(Boolean)
    .map((segment) => segment.toLowerCase())
  const fileName = segments.at(-1) ?? ''
  const root = segments[0] ?? ''
  const runtimeSection = root === 'runtime' ? segments[1] ?? '' : ''

  if (
    segments.some((segment) => segment === 'backup' || segment === 'backups') ||
    segments.some((segment) => segment.endsWith('_backups')) ||
    /\.(?:bak|backup)$/.test(fileName)
  ) {
    return 'backups'
  }
  if (segments.some((segment) => TOOL_CACHE_SEGMENTS.has(segment))) {
    return 'cache'
  }
  if (
    root === 'data' ||
    (root === 'runtime' &&
      (TOOL_USER_DATA_RUNTIME_ROOTS.has(runtimeSection) ||
        runtimeSection.startsWith('test-self-training')))
  ) {
    return 'user_data'
  }
  if (TOOL_RUNTIME_ROOTS.has(root) || root === 'runtime') {
    return 'runtime'
  }
  return 'program'
}

async function folderSize(
  folderPath: string,
  options: {
    excludedRootDirectories?: ReadonlySet<string>
    includeToolBreakdown?: boolean
  } = {}
): Promise<{
  bytes: number
  fileCount: number
  breakdown?: ToolSizeBreakdown
}> {
  const pending = [folderPath]
  const resolvedRoot = path.resolve(folderPath)
  let bytes = 0
  let fileCount = 0
  let visitedEntries = 0
  const breakdown = options.includeToolBreakdown
    ? emptyToolSizeBreakdown()
    : undefined

  while (pending.length > 0) {
    const current = pending.pop()
    if (!current) continue

    let entries: fs.Dirent[]
    try {
      entries = await fs.promises.readdir(current, { withFileTypes: true })
    } catch {
      continue
    }

    for (const entry of entries) {
      visitedEntries += 1
      if (visitedEntries % YIELD_EVERY_ENTRIES === 0) {
        await yieldToEventLoop()
      }
      if (entry.isSymbolicLink()) continue

      const entryPath = path.join(current, entry.name)
      if (entry.isDirectory()) {
        if (
          path.resolve(current) === resolvedRoot &&
          options.excludedRootDirectories?.has(entry.name)
        ) {
          continue
        }
        pending.push(entryPath)
        continue
      }
      if (!entry.isFile()) continue

      try {
        const stat = await fs.promises.lstat(entryPath)
        if (!stat.isFile() || stat.isSymbolicLink()) continue
        bytes += stat.size
        fileCount += 1
        if (breakdown) {
          const category = classifyToolFile(path.relative(resolvedRoot, entryPath))
          breakdown[category].size_bytes += stat.size
          breakdown[category].file_count += 1
        }
      } catch {
        // Files may disappear during cleanup; omit the transient entry.
      }
    }
  }

  return {
    bytes: Math.max(0, Math.min(bytes, Number.MAX_SAFE_INTEGER)),
    fileCount,
    breakdown,
  }
}

async function buildInventory(workspaceRoot: string): Promise<PlatformToolSize[]> {
  const resolvedRoot = path.resolve(workspaceRoot)
  let entries: fs.Dirent[]
  try {
    entries = await fs.promises.readdir(resolvedRoot, { withFileTypes: true })
  } catch {
    return []
  }

  const tools: PlatformToolSize[] = []
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue
    const folderPath = path.resolve(resolvedRoot, entry.name)
    if (!isPathInside(resolvedRoot, folderPath)) continue

    const manifestPath = path.join(folderPath, 'manifest.json')
    let manifest: Record<string, unknown>
    try {
      manifest = JSON.parse(
        await fs.promises.readFile(manifestPath, 'utf-8')
      ) as Record<string, unknown>
    } catch {
      continue
    }

    const id = String(manifest.id ?? '').trim()
    if (!/^[a-z0-9_-]+$/.test(id) || id !== entry.name) continue
    const runtime =
      manifest.runtime && typeof manifest.runtime === 'object'
        ? (manifest.runtime as Record<string, unknown>)
        : {}
    const rawEntry = String(runtime.entry ?? manifest.entry ?? '').trim()
    const codePath = rawEntry
      ? path.resolve(folderPath, rawEntry)
      : folderPath
    const safeCodePath = isPathInside(folderPath, codePath) ? codePath : folderPath
    const size = await folderSize(folderPath, { includeToolBreakdown: true })
    tools.push({
      id,
      folder_path: folderPath,
      manifest_path: manifestPath,
      code_path: safeCodePath,
      project_size_bytes: size.bytes,
      file_count: size.fileCount,
      size_breakdown: size.breakdown ?? emptyToolSizeBreakdown(),
    })
  }
  return tools
}

export async function getPlatformToolSizes(
  workspaceRoot: string,
  forceRefresh = false
): Promise<PlatformToolSize[]> {
  const resolvedRoot = path.resolve(workspaceRoot)
  const now = Date.now()
  if (
    !forceRefresh &&
    cachedInventory &&
    cachedInventory.workspaceRoot === resolvedRoot &&
    cachedInventory.expiresAt > now
  ) {
    return cachedInventory.tools.map((tool) => ({ ...tool }))
  }
  if (inventoryPromise) return inventoryPromise

  inventoryPromise = buildInventory(resolvedRoot)
  try {
    const tools = await inventoryPromise
    cachedInventory = {
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS,
      tools,
    }
    return tools.map((tool) => ({ ...tool }))
  } finally {
    inventoryPromise = null
  }
}

export async function getMainSystemSize(
  workspaceRoot: string,
  forceRefresh = false
): Promise<MainSystemSize> {
  const resolvedRoot = path.resolve(workspaceRoot)
  const mainSystemRoot = path.resolve(resolvedRoot, 'main-system')
  if (!isPathInside(resolvedRoot, mainSystemRoot)) {
    throw new Error('Main-system folder escaped workspace boundary')
  }
  const now = Date.now()
  if (
    !forceRefresh &&
    cachedMainSystemSize &&
    cachedMainSystemSize.workspaceRoot === resolvedRoot &&
    cachedMainSystemSize.expiresAt > now
  ) {
    return {
      folder_path: cachedMainSystemSize.folder_path,
      project_size_bytes: cachedMainSystemSize.project_size_bytes,
      file_count: cachedMainSystemSize.file_count,
      dependency_size_bytes: cachedMainSystemSize.dependency_size_bytes,
      dependency_file_count: cachedMainSystemSize.dependency_file_count,
      total_size_bytes: cachedMainSystemSize.total_size_bytes,
      total_file_count: cachedMainSystemSize.total_file_count,
    }
  }
  if (mainSystemSizePromise) return mainSystemSizePromise

  mainSystemSizePromise = (async () => {
    const size = await folderSize(mainSystemRoot, {
      excludedRootDirectories: MAIN_SYSTEM_DEPENDENCY_DIRECTORIES,
    })
    let dependencySizeBytes = 0
    let dependencyFileCount = 0
    for (const dependencyDirectory of MAIN_SYSTEM_DEPENDENCY_DIRECTORIES) {
      const dependencySize = await folderSize(
        path.join(mainSystemRoot, dependencyDirectory)
      )
      dependencySizeBytes += dependencySize.bytes
      dependencyFileCount += dependencySize.fileCount
    }
    const result: MainSystemSize = {
      folder_path: mainSystemRoot,
      project_size_bytes: size.bytes,
      file_count: size.fileCount,
      dependency_size_bytes: dependencySizeBytes,
      dependency_file_count: dependencyFileCount,
      total_size_bytes: size.bytes + dependencySizeBytes,
      total_file_count: size.fileCount + dependencyFileCount,
    }
    cachedMainSystemSize = {
      ...result,
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS,
    }
    return result
  })()
  try {
    return await mainSystemSizePromise
  } finally {
    mainSystemSizePromise = null
  }
}

export async function getSharedLayerSize(
  workspaceRoot: string,
  forceRefresh = false
): Promise<SharedLayerSize> {
  const resolvedRoot = path.resolve(workspaceRoot)
  const sharedLayerRoot = path.resolve(resolvedRoot, 'shared-layer')
  if (!isPathInside(resolvedRoot, sharedLayerRoot)) {
    throw new Error('Shared-layer folder escaped workspace boundary')
  }
  const now = Date.now()
  if (
    !forceRefresh &&
    cachedSharedLayerSize &&
    cachedSharedLayerSize.workspaceRoot === resolvedRoot &&
    cachedSharedLayerSize.expiresAt > now
  ) {
    return {
      folder_path: cachedSharedLayerSize.folder_path,
      project_size_bytes: cachedSharedLayerSize.project_size_bytes,
      file_count: cachedSharedLayerSize.file_count,
    }
  }
  if (sharedLayerSizePromise) return sharedLayerSizePromise

  sharedLayerSizePromise = (async () => {
    const size = await folderSize(sharedLayerRoot)
    const result: SharedLayerSize = {
      folder_path: sharedLayerRoot,
      project_size_bytes: size.bytes,
      file_count: size.fileCount,
    }
    cachedSharedLayerSize = {
      ...result,
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS,
    }
    return result
  })()
  try {
    return await sharedLayerSizePromise
  } finally {
    sharedLayerSizePromise = null
  }
}

export async function getWorkspaceSize(
  workspaceRoot: string,
  tools: PlatformToolSize[],
  mainSystem: MainSystemSize,
  sharedLayer: SharedLayerSize,
  forceRefresh = false
): Promise<WorkspaceSize> {
  const resolvedRoot = path.resolve(workspaceRoot)
  const now = Date.now()
  if (
    !forceRefresh &&
    cachedWorkspaceSize &&
    cachedWorkspaceSize.workspaceRoot === resolvedRoot &&
    cachedWorkspaceSize.expiresAt > now
  ) {
    return {
      folder_path: cachedWorkspaceSize.folder_path,
      project_size_bytes: cachedWorkspaceSize.project_size_bytes,
      file_count: cachedWorkspaceSize.file_count,
    }
  }
  if (workspaceSizePromise) return workspaceSizePromise

  workspaceSizePromise = (async () => {
    const measuredRoots = new Map<string, { bytes: number; fileCount: number }>()
    measuredRoots.set(path.resolve(mainSystem.folder_path), {
      bytes: mainSystem.total_size_bytes,
      fileCount: mainSystem.total_file_count,
    })
    measuredRoots.set(path.resolve(sharedLayer.folder_path), {
      bytes: sharedLayer.project_size_bytes,
      fileCount: sharedLayer.file_count,
    })
    for (const tool of tools) {
      measuredRoots.set(path.resolve(tool.folder_path), {
        bytes: tool.project_size_bytes,
        fileCount: tool.file_count,
      })
    }

    let bytes = 0
    let fileCount = 0
    let entries: fs.Dirent[] = []
    try {
      entries = await fs.promises.readdir(resolvedRoot, { withFileTypes: true })
    } catch {
      // Keep the result unavailable instead of returning a false zero.
      throw new Error('Workspace folder cannot be read')
    }

    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue
      const entryPath = path.resolve(resolvedRoot, entry.name)
      if (!isPathInside(resolvedRoot, entryPath)) continue
      if (entry.isDirectory()) {
        const known = measuredRoots.get(entryPath)
        const size = known ?? (await folderSize(entryPath))
        bytes += size.bytes
        fileCount += size.fileCount
      } else if (entry.isFile()) {
        try {
          const stat = await fs.promises.lstat(entryPath)
          if (!stat.isSymbolicLink() && stat.isFile()) {
            bytes += stat.size
            fileCount += 1
          }
        } catch {
          // A cleanup may remove a root file during the scan.
        }
      }
    }

    if (fileCount <= 0 || bytes <= 0) {
      throw new Error('Workspace size inventory is empty')
    }
    const result: WorkspaceSize = {
      folder_path: resolvedRoot,
      project_size_bytes: Math.min(bytes, Number.MAX_SAFE_INTEGER),
      file_count: fileCount,
    }
    cachedWorkspaceSize = {
      ...result,
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS,
    }
    return result
  })()
  try {
    return await workspaceSizePromise
  } finally {
    workspaceSizePromise = null
  }
}
