import { toolboxToolRegistry } from './registry'
import type {
  ToolAction,
  ToolRuntimeState,
  ToolRuntimeStatus,
} from './types'

interface ToolboxManifestPayload {
  id?: unknown
  name?: unknown
  description?: unknown
  status?: unknown
  enabled?: unknown
  folder_path?: unknown
  manifest_path?: unknown
  code_path?: unknown
  executable_path?: unknown
  executable_exists?: unknown
  project_size_bytes?: unknown
  has_custom_ui?: unknown
  hidden_from_toolbox?: unknown
  merged_into?: unknown
}

interface ToolboxProjectSizePayload {
  id?: unknown
  folder_path?: unknown
  manifest_path?: unknown
  code_path?: unknown
  project_size_bytes?: unknown
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
  if (!launchable) return '此工具目前不可由主程式啟動。'
  if (status === 'running') return '工具已連線並正常執行。'
  if (status === 'starting') return '正在建立獨立工具程序與驗證連線。'
  if (status === 'stopping') return '正在安全停止獨立工具程序。'
  if (status === 'error') return '工具回報異常，請查看訊息或執行自動修正。'
  return '工具已就緒，可由主程式啟動。'
}

function normalizeSizeBytes(value: unknown): number | undefined {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined
}

function normalizeBoolean(value: unknown): boolean {
  return value === true || String(value ?? '').trim().toLowerCase() === 'true'
}

function isVisibleTool(tool: {
  hiddenFromToolbox?: boolean
  hidden_from_toolbox?: boolean
  mergedInto?: string
  merged_into?: string
}): boolean {
  return (
    tool.hiddenFromToolbox !== true &&
    tool.hidden_from_toolbox !== true &&
    String(tool.mergedInto ?? tool.merged_into ?? '').trim() === ''
  )
}

export function createInitialToolboxRuntimeState(): ToolRuntimeState[] {
  const now = Date.now()
  return toolboxToolRegistry.filter(isVisibleTool).map((tool) => ({
    ...tool,
    description: tool.description || tool.summary,
    hasCustomUi: tool.hasCustomUi === true || tool.has_custom_ui === true,
    status: 'stopped',
    updatedAt: now,
    note: noteForStatus('stopped', tool.launchable !== false),
  }))
}

export function hydrateToolboxRuntimeStateFromBackend(
  payload: unknown
): ToolRuntimeState[] {
  const list = Array.isArray(payload) ? payload : []
  const now = Date.now()
  const registryById = new Map(toolboxToolRegistry.map((tool) => [tool.id, tool]))
  const tools: ToolRuntimeState[] = []

  for (const entry of list) {
    const manifest = entry as ToolboxManifestPayload
    const id = String(manifest.id ?? '').trim()
    if (!id) continue
    const registryTool = registryById.get(id)
    if (
      normalizeBoolean(manifest.hidden_from_toolbox) ||
      String(manifest.merged_into ?? '').trim() !== '' ||
      (registryTool && !isVisibleTool(registryTool))
    ) {
      continue
    }

    const launchable = manifest.enabled !== false && registryTool?.launchable !== false
    const status = launchable ? normalizeStatus(manifest.status) : 'stopped'
    const description = String(
      manifest.description ?? registryTool?.description ?? registryTool?.summary ?? ''
    ).trim()
    const name = String(manifest.name ?? registryTool?.name ?? id).trim() || id
    const executableExists =
      typeof manifest.executable_exists === 'boolean'
        ? manifest.executable_exists
        : registryTool?.executableExists
    const note =
      executableExists === false
        ? '找不到工具 EXE，請重新執行獨立工具封裝。'
        : noteForStatus(status, launchable)
    const summary = description || registryTool?.summary || `獨立工具：${id}`

    tools.push({
      id,
      name,
      summary,
      description: description || summary,
      folderPath: String(manifest.folder_path ?? registryTool?.folderPath ?? '').trim(),
      manifestPath: String(manifest.manifest_path ?? registryTool?.manifestPath ?? '').trim(),
      codePath: String(manifest.code_path ?? registryTool?.codePath ?? '').trim(),
      executablePath: String(
        manifest.executable_path ?? registryTool?.executablePath ?? ''
      ).trim(),
      executableExists,
      projectSizeBytes:
        normalizeSizeBytes(manifest.project_size_bytes) ??
        normalizeSizeBytes(registryTool?.projectSizeBytes),
      hasCustomUi:
        normalizeBoolean(manifest.has_custom_ui) ||
        registryTool?.hasCustomUi === true ||
        registryTool?.has_custom_ui === true,
      launchable,
      windowOnly: registryTool?.windowOnly,
      status,
      updatedAt: now,
      note,
    })
  }

  for (const reserved of toolboxToolRegistry.filter(isVisibleTool)) {
    if (tools.some((tool) => tool.id === reserved.id)) continue
    tools.push({
      ...reserved,
      description: reserved.description || reserved.summary,
      hasCustomUi: reserved.hasCustomUi === true || reserved.has_custom_ui === true,
      status: 'stopped',
      updatedAt: now,
      note: noteForStatus('stopped', reserved.launchable !== false),
    })
  }

  return tools
}

export function mergeToolboxProjectSizes(
  tools: ToolRuntimeState[],
  payload: unknown
): ToolRuntimeState[] {
  const list = Array.isArray(payload) ? payload : []
  const sizesById = new Map<string, ToolboxProjectSizePayload & { projectSizeBytes: number }>()

  for (const raw of list) {
    const item = raw as ToolboxProjectSizePayload
    const id = String(item.id ?? '').trim()
    const projectSizeBytes = normalizeSizeBytes(item.project_size_bytes)
    if (id && projectSizeBytes !== undefined) {
      sizesById.set(id, { ...item, projectSizeBytes })
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
