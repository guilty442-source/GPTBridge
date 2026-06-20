import { zhTW } from '@/i18n/zhTW'
import type {
  ToolAction,
  ToolRuntimeState,
  ToolRuntimeStatus,
} from '@/ui/developer-mode/tools/types'
import { toolboxToolRegistry } from './registry'

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
  if (value === 'error' || value === 'fail' || value === 'failed') return 'error'
  return 'stopped'
}

function noteForStatus(status: ToolRuntimeStatus, launchable: boolean): string {
  if (!launchable) return '此功能在主程式內開啟，無需單獨啟動。'
  if (status === 'running') return zhTW.toolbox.status_running
  if (status === 'starting') return zhTW.toolbox.status_starting
  if (status === 'stopping') return zhTW.toolbox.status_stopping
  if (status === 'error') return zhTW.toolbox.status_error
  return zhTW.toolbox.status_stopped
}

function normalizeSizeBytes(value: unknown): number | undefined {
  if (typeof value === 'number') {
    return Number.isFinite(value) && value >= 0 ? value : undefined
  }

  if (typeof value === 'string') {
    const parsed = Number(value.trim())
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined
  }

  return undefined
}

function normalizeBoolean(value: unknown): boolean {
  if (value === true) return true
  if (typeof value === 'string') {
    return value.trim().toLowerCase() === 'true'
  }
  return false
}

export function createInitialToolboxRuntimeState(): ToolRuntimeState[] {
  const now = Date.now()
  return toolboxToolRegistry
    .filter(
      (tool) =>
        tool.hiddenFromToolbox !== true &&
        tool.hidden_from_toolbox !== true &&
        String(tool.mergedInto ?? tool.merged_into ?? '').trim() === ''
    )
    .map((tool) => ({
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
      registryTool?.hiddenFromToolbox === true ||
      registryTool?.hidden_from_toolbox === true ||
      String(registryTool?.mergedInto ?? registryTool?.merged_into ?? '').trim() !== ''
    ) {
      continue
    }
    const launchable = manifest.enabled !== false
    const effectiveLaunchable =
      registryTool?.launchable === false ? false : launchable
    const status = effectiveLaunchable
      ? normalizeStatus(manifest.status)
      : 'stopped'
    const description = String(
      manifest.description ?? registryTool?.description ?? ''
    ).trim()
    const name = String(manifest.name ?? registryTool?.name ?? id).trim() || id
    const folderPath = String(
      manifest.folder_path ?? registryTool?.folderPath ?? ''
    ).trim()
    const manifestPath = String(
      manifest.manifest_path ?? registryTool?.manifestPath ?? ''
    ).trim()
    const codePath = String(
      manifest.code_path ?? registryTool?.codePath ?? ''
    ).trim()
    const executablePath = String(
      manifest.executable_path ?? registryTool?.executablePath ?? ''
    ).trim()
    const executableExists =
      typeof manifest.executable_exists === 'boolean'
        ? manifest.executable_exists
        : registryTool?.executableExists
    const projectSize =
      normalizeSizeBytes(manifest.project_size_bytes) ??
      normalizeSizeBytes(registryTool?.projectSizeBytes)
    const hasCustomUi =
      normalizeBoolean(manifest.has_custom_ui) ||
      registryTool?.hasCustomUi === true ||
      registryTool?.has_custom_ui === true
    const note =
      executableExists === false
        ? '找不到 EXE，請先執行 npm run package:tool。'
        : noteForStatus(status, effectiveLaunchable)
    const summary = description || registryTool?.summary || `已載入工具：${id}`

    tools.push({
      id,
      name,
      summary,
      description: description || summary,
      folderPath,
      manifestPath,
      codePath,
      executablePath,
      executableExists,
      projectSizeBytes: projectSize,
      hasCustomUi,
      launchable: effectiveLaunchable,
      windowOnly: registryTool?.windowOnly,
      status,
      updatedAt: now,
      note,
    })
  }

  for (const reserved of toolboxToolRegistry) {
    if (
      reserved.hiddenFromToolbox === true ||
      reserved.hidden_from_toolbox === true ||
      String(reserved.mergedInto ?? reserved.merged_into ?? '').trim() !== ''
    ) {
      continue
    }
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
  const sizesById = new Map<
    string,
    {
      folderPath?: string
      manifestPath?: string
      codePath?: string
      projectSizeBytes: number
    }
  >()

  for (const entry of list) {
    const item = entry as ToolboxProjectSizePayload
    const id = String(item.id ?? '').trim()
    const projectSizeBytes = normalizeSizeBytes(item.project_size_bytes)
    if (!id || projectSizeBytes === undefined) continue

    sizesById.set(id, {
      folderPath: String(item.folder_path ?? '').trim() || undefined,
      manifestPath: String(item.manifest_path ?? '').trim() || undefined,
      codePath: String(item.code_path ?? '').trim() || undefined,
      projectSizeBytes,
    })
  }

  if (sizesById.size === 0) return tools

  return tools.map((tool) => {
    const size = sizesById.get(tool.id)
    if (!size) return tool

    return {
      ...tool,
      folderPath: size.folderPath || tool.folderPath,
      manifestPath: size.manifestPath || tool.manifestPath,
      codePath: size.codePath || tool.codePath,
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
    if (tool.id !== toolId) {
      return tool
    }

    if (tool.launchable === false) {
      return {
        ...tool,
        status: 'stopped',
        updatedAt: now,
        note: noteForStatus('stopped', false),
      }
    }

    if (phase === 'pending') {
      const nextStatus = action === 'start' ? 'starting' : 'stopping'
      return {
        ...tool,
        status: nextStatus,
        updatedAt: now,
        note: noteForStatus(nextStatus, true),
      }
    }

    const nextStatus = action === 'start' ? 'running' : 'stopped'
    return {
      ...tool,
      status: nextStatus,
      updatedAt: now,
      note: noteForStatus(nextStatus, true),
    }
  })
}
