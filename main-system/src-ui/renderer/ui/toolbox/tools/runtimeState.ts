import type {
  ToolAction,
  ToolCapacityBreakdown,
  ToolDataBoundary,
  ToolRuntimeMode,
  ToolRuntimeState,
  ToolRuntimeStatus,
} from './types'
import { mainSystemLocale } from '@/locales/main-system'

const t = mainSystemLocale.toolbox

interface ToolboxManifestPayload {
  id?: unknown
  name?: unknown
  description?: unknown
  status?: unknown
  enabled?: unknown
  permission_denied?: unknown
  lifecycle_locked?: unknown
  governance_authority?: unknown
  folder_path?: unknown
  manifest_path?: unknown
  code_path?: unknown
  executable_path?: unknown
  executable_exists?: unknown
  runtime_available?: unknown
  runtime_mode?: unknown
  automatic_runtime_mode?: unknown
  project_size_bytes?: unknown
  file_count?: unknown
  size_breakdown?: unknown
  standalone?: unknown
  data_boundary?: unknown
  has_custom_ui?: unknown
  hidden_from_toolbox?: unknown
  merged_into?: unknown
  window_only?: unknown
}

interface ToolboxProjectSizePayload {
  id?: unknown
  folder_path?: unknown
  manifest_path?: unknown
  code_path?: unknown
  project_size_bytes?: unknown
  file_count?: unknown
  size_breakdown?: unknown
}

function normalizeStatus(status: unknown): ToolRuntimeStatus {
  const value = String(status ?? '').toLowerCase()
  if (value === 'running') return 'running'
  if (value === 'starting') return 'starting'
  if (value === 'stopping') return 'stopping'
  if (['error', 'fail', 'failed'].includes(value)) return 'error'
  return 'stopped'
}

function noteForStatus(status: ToolRuntimeStatus, launchable: boolean): string {
  if (!launchable) return t.notLaunchable
  if (status === 'running') return t.runningNote
  if (status === 'starting') return t.startingNote
  if (status === 'stopping') return t.stoppingNote
  if (status === 'error') return t.errorNote
  return t.readyNote
}

function normalizeSizeBytes(value: unknown): number | undefined {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined
}

function normalizeFileCount(value: unknown): number | undefined {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined
}

function normalizeCapacityBreakdown(value: unknown): ToolCapacityBreakdown | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Record<string, unknown>
  const normalizeCategory = (
    category: unknown
  ): { sizeBytes: number; fileCount: number } | undefined => {
    if (!category || typeof category !== 'object') return undefined
    const fields = category as Record<string, unknown>
    const sizeBytes = normalizeSizeBytes(fields.size_bytes)
    const fileCount = normalizeFileCount(fields.file_count)
    return sizeBytes === undefined || fileCount === undefined
      ? undefined
      : { sizeBytes, fileCount }
  }
  const program = normalizeCategory(raw.program)
  const runtime = normalizeCategory(raw.runtime)
  const userData = normalizeCategory(raw.user_data)
  const cache = normalizeCategory(raw.cache)
  const backups = normalizeCategory(raw.backups)
  return program && runtime && userData && cache && backups
    ? { program, runtime, userData, cache, backups }
    : undefined
}

function normalizeRuntimeMode(value: unknown): ToolRuntimeMode | undefined {
  const mode = String(value ?? '').trim().toLowerCase()
  if (
    mode === 'executable' ||
    mode === 'governed-source' ||
    mode === 'dual-runtime'
  ) return mode
  return undefined
}

function normalizeAutomaticRuntimeMode(
  value: unknown
): 'executable' | 'governed-source' | undefined {
  const mode = String(value ?? '').trim().toLowerCase()
  return mode === 'executable' || mode === 'governed-source' ? mode : undefined
}

function normalizeDataBoundary(manifest: ToolboxManifestPayload): ToolDataBoundary {
  const raw = manifest.data_boundary
  const boundary = raw && typeof raw === 'object'
    ? raw as Record<string, unknown>
    : {}
  return {
    standalone:
      typeof boundary.standalone === 'boolean'
        ? boundary.standalone
        : normalizeBoolean(manifest.standalone),
    codeScope: String(boundary.code_scope ?? '').trim() || undefined,
    databaseScope: String(boundary.database_scope ?? '').trim() || undefined,
  }
}

function normalizeBoolean(value: unknown): boolean {
  return value === true || String(value ?? '').trim().toLowerCase() === 'true'
}

export function createInitialToolboxRuntimeState(): ToolRuntimeState[] {
  return []
}

export function hydrateToolboxRuntimeStateFromBackend(
  payload: unknown
): ToolRuntimeState[] {
  const list = Array.isArray(payload) ? payload : []
  const now = Date.now()
  const tools: ToolRuntimeState[] = []

  for (const entry of list) {
    const manifest = entry as ToolboxManifestPayload
    const id = String(manifest.id ?? '').trim()
    if (!id) continue
    if (
      normalizeBoolean(manifest.hidden_from_toolbox) ||
      String(manifest.merged_into ?? '').trim() !== ''
    ) {
      continue
    }

    const permissionDenied = normalizeBoolean(manifest.permission_denied)
    const lifecycleLocked = normalizeBoolean(manifest.lifecycle_locked)
    const governanceAuthority = normalizeBoolean(manifest.governance_authority)
    const launchable = manifest.enabled !== false && !permissionDenied
    const status = lifecycleLocked
      ? normalizeStatus(manifest.status)
      : launchable
        ? normalizeStatus(manifest.status)
        : 'stopped'
    const description = String(manifest.description ?? '').trim()
    const name = String(manifest.name ?? id).trim() || id
    const executableExists =
      typeof manifest.executable_exists === 'boolean'
        ? manifest.executable_exists
        : undefined
    const runtimeAvailable =
      typeof manifest.runtime_available === 'boolean'
        ? manifest.runtime_available
        : executableExists
    const runtimeMode = normalizeRuntimeMode(manifest.runtime_mode)
    const note = governanceAuthority
      ? t.runningNote
      : permissionDenied
        ? mainSystemLocale['errors.permission_denied']
      : runtimeAvailable === false
        ? t.executableMissing
        : noteForStatus(status, launchable)
    const summary = description || `${t.independentToolPrefix}：${id}`

    tools.push({
      id,
      name,
      summary,
      description: description || summary,
      folderPath: String(manifest.folder_path ?? '').trim(),
      manifestPath: String(manifest.manifest_path ?? '').trim(),
      codePath: String(manifest.code_path ?? '').trim(),
      executablePath: String(manifest.executable_path ?? '').trim(),
      executableExists,
      runtimeAvailable,
      runtimeMode,
      automaticRuntimeMode: normalizeAutomaticRuntimeMode(
        manifest.automatic_runtime_mode
      ),
      projectSizeBytes: normalizeSizeBytes(manifest.project_size_bytes),
      projectFileCount: normalizeFileCount(manifest.file_count),
      capacityBreakdown: normalizeCapacityBreakdown(manifest.size_breakdown),
      dataBoundary: normalizeDataBoundary(manifest),
      hasCustomUi: normalizeBoolean(manifest.has_custom_ui),
      launchable,
      lifecycleLocked,
      windowOnly: normalizeBoolean(manifest.window_only),
      status,
      updatedAt: now,
      note,
    })
  }

  return tools
}

export function mergeToolboxProjectSizes(
  tools: ToolRuntimeState[],
  payload: unknown
): ToolRuntimeState[] {
  const list = Array.isArray(payload) ? payload : []
  const sizesById = new Map<
    string,
    ToolboxProjectSizePayload & {
      projectSizeBytes: number
      projectFileCount?: number
      capacityBreakdown?: ToolCapacityBreakdown
    }
  >()

  for (const raw of list) {
    const item = raw as ToolboxProjectSizePayload
    const id = String(item.id ?? '').trim()
    const projectSizeBytes = normalizeSizeBytes(item.project_size_bytes)
    if (id && projectSizeBytes !== undefined) {
      sizesById.set(id, {
        ...item,
        projectSizeBytes,
        projectFileCount: normalizeFileCount(item.file_count),
        capacityBreakdown: normalizeCapacityBreakdown(item.size_breakdown),
      })
    }
  }

  return tools.map((tool) => {
    const size = sizesById.get(tool.id)
    if (!size) return tool
    return {
      ...tool,
      folderPath: String(size.folder_path ?? '').trim() || tool.folderPath,
      manifestPath: String(size.manifest_path ?? '').trim() || tool.manifestPath,
      codePath: String(size.code_path ?? '').trim() || tool.codePath,
      projectSizeBytes: size.projectSizeBytes,
      projectFileCount: size.projectFileCount,
      capacityBreakdown: size.capacityBreakdown,
    }
  })
}

export function resolveToolboxToolAction(
  tools: ToolRuntimeState[],
  toolId: string,
  action: ToolAction,
  phase: 'pending' | 'settled'
): ToolRuntimeState[] {
  const now = Date.now()
  return tools.map((tool) => {
    if (tool.id !== toolId) return tool
    if (tool.launchable === false) {
      return {
        ...tool,
        status: 'stopped',
        updatedAt: now,
        note: noteForStatus('stopped', false),
      }
    }
    const status: ToolRuntimeStatus =
      phase === 'pending'
        ? action === 'start'
          ? 'starting'
          : 'stopping'
        : action === 'start'
          ? 'running'
          : 'stopped'
    return { ...tool, status, updatedAt: now, note: noteForStatus(status, true) }
  })
}
