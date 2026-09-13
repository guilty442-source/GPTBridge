export type PlatformToolSize = {
  id: string
  folder_path: string
  manifest_path: string
  code_path: string
  project_size_bytes: number
  file_count: number
  size_breakdown: ToolSizeBreakdown
}

export type ToolSizeCategory =
  | 'program'
  | 'runtime'
  | 'user_data'
  | 'cache'
  | 'backups'

export type ToolCategorySize = {
  size_bytes: number
  file_count: number
}

export type ToolSizeBreakdown = Record<ToolSizeCategory, ToolCategorySize>

export type CachedInventory = {
  workspaceRoot: string
  expiresAt: number
  tools: PlatformToolSize[]
}

export type FolderInventorySize = {
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

export type CachedMainSystemSize = MainSystemSize & {
  workspaceRoot: string
  expiresAt: number
}

export type CachedFolderInventorySize = FolderInventorySize & {
  workspaceRoot: string
  expiresAt: number
}

export const CACHE_TTL_MS = 30_000
export const YIELD_EVERY_ENTRIES = 256
export const MAIN_SYSTEM_DEPENDENCY_DIRECTORIES = new Set(['.venv', 'node_modules'])
export const TOOL_RUNTIME_ROOTS = new Set([
  '.venv',
  'build',
  'dist',
  'env',
  'node_modules',
  'release',
  'venv',
])
export const TOOL_CACHE_SEGMENTS = new Set([
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
export const TOOL_USER_DATA_RUNTIME_ROOTS = new Set([
  'data',
  'recovery',
  'settings',
  'state',
])

export function emptyToolSizeBreakdown(): ToolSizeBreakdown {
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
