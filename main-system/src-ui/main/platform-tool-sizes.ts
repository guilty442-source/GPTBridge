import fs from 'node:fs'
import path from 'node:path'

import {
  type PlatformToolSize,
  type ToolSizeBreakdown,
  type CachedInventory,
  type FolderInventorySize,
  type MainSystemSize,
  type WorkspaceSize,
  type SharedLayerSize,
  type CachedMainSystemSize,
  type CachedFolderInventorySize,
  CACHE_TTL_MS,
  YIELD_EVERY_ENTRIES,
  MAIN_SYSTEM_DEPENDENCY_DIRECTORIES,
  emptyToolSizeBreakdown,
  classifyToolFile,
} from './platformToolSizeTypes'

export {
  type PlatformToolSize,
  type ToolSizeBreakdown,
  type MainSystemSize,
  type WorkspaceSize,
  type SharedLayerSize,
  classifyToolFile,
}

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

async function listDirectChildFolders(root: string): Promise<string[]> {
  try {
    const entries = await fs.promises.readdir(root, { withFileTypes: true })
    return entries
      .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
      .map((entry) => path.join(root, entry.name))
  } catch {
    return []
  }
}

async function collectToolManifestCandidates(
  resolvedRoot: string
): Promise<Array<{ folderPath: string; manifestPath: string }>> {
  const candidates: Array<{ folderPath: string; manifestPath: string }> = []
  const seen = new Set<string>()
  const push = (folderPath: string) => {
    const manifestPath = path.join(folderPath, 'manifest.json')
    if (seen.has(manifestPath)) return
    seen.add(manifestPath)
    candidates.push({ folderPath, manifestPath })
  }
  for (const folderPath of await listDirectChildFolders(resolvedRoot)) push(folderPath)
  // Independent tools live under "Standalone tools/"; declared nested tools
  // (hosted entities) live inside a host root at any depth.
  const toolsRoot = path.join(resolvedRoot, 'Standalone tools')
  for (const hostFolder of await listDirectChildFolders(toolsRoot)) {
    push(hostFolder)
    for (const nestedFolder of await listDirectChildFolders(hostFolder)) {
      push(nestedFolder)
      for (const deeperFolder of await listDirectChildFolders(nestedFolder)) {
        push(deeperFolder)
      }
    }
  }
  return candidates
}

async function readToolManifest(manifestPath: string): Promise<Record<string, unknown> | null> {
  try {
    return JSON.parse(
      await fs.promises.readFile(manifestPath, 'utf-8')
    ) as Record<string, unknown>
  } catch {
    return null
  }
}

function declaresIndependentToolCard(manifest: Record<string, unknown>): boolean {
  // A manifest owns a toolbox card only when it declares itself as an
  // independent tool; infrastructure (codex, shared layer), utilities and
  // companions declare their own status through the manifest.
  if (manifest.main_system_independent_tool !== true) return false
  if (manifest.companion_tool === true) return false
  if (manifest.hidden_from_toolbox === true) return false
  return true
}

async function buildInventory(workspaceRoot: string): Promise<PlatformToolSize[]> {
  const resolvedRoot = path.resolve(workspaceRoot)
  const candidates = await collectToolManifestCandidates(resolvedRoot)

  const tools: PlatformToolSize[] = []
  for (const candidate of candidates) {
    const folderPath = path.resolve(candidate.folderPath)
    if (!isPathInside(resolvedRoot, folderPath)) continue

    const manifest = await readToolManifest(candidate.manifestPath)
    if (!manifest || !declaresIndependentToolCard(manifest)) continue

    const id = String(manifest.id ?? '').trim()
    if (!/^[a-z0-9_-]+$/.test(id) || id !== path.basename(folderPath)) continue
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
      manifest_path: candidate.manifestPath,
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
