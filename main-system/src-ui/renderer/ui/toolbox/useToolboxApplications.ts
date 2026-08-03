import { useCallback, useEffect, useRef, useState } from 'react'
import type { ToolAction, ToolRuntimeState } from './tools/types'
import {
  createInitialToolboxRuntimeState,
  hydrateToolboxRuntimeStateFromBackend,
  mergeToolboxProjectSizes,
  resolveToolboxToolAction,
} from '@/ui/toolbox/tools/runtimeState'
import { mainSystemLocale } from '@/locales/main-system'

const t = mainSystemLocale.toolbox

type SendCommandResult = { ok: boolean; queued: boolean; message?: string }
type SendCommand = (command: string, payload?: unknown) => SendCommandResult
type WaitForIpcEvent = (
  eventName: string,
  timeoutMs: number,
  predicate?: (payload: Record<string, unknown>) => boolean
) => Promise<Record<string, unknown>>

type UseToolboxApplicationsOptions = {
  backendStatus: string
  sendCommand: SendCommand
  waitForIpcEvent: WaitForIpcEvent
}

type PlatformToolSizesPayload = {
  ok?: boolean
  tools?: unknown
  main_system?: {
    project_size_bytes?: unknown
    file_count?: unknown
    dependency_size_bytes?: unknown
    dependency_file_count?: unknown
    total_size_bytes?: unknown
    total_file_count?: unknown
  }
  shared_layer?: { project_size_bytes?: unknown; file_count?: unknown }
  workspace?: { project_size_bytes?: unknown; file_count?: unknown }
}

const TOOLBOX_LIST_TIMEOUT_MS = 20000
const TOOLBOX_ACTION_TIMEOUT_MS = 120000
const LOCAL_TOOL_SIZE_RETRY_LIMIT = 6
const LOCAL_TOOL_SIZE_RETRY_MS = 1500

function hasMissingProjectSizes(tools: ToolRuntimeState[]): boolean {
  return tools.some(
    (tool) =>
      Boolean(tool.folderPath) &&
      (typeof tool.projectSizeBytes !== 'number' ||
        !Number.isFinite(tool.projectSizeBytes))
  )
}

function normalizeInventoryCount(value: unknown): number | undefined {
  const count = typeof value === 'number' ? value : Number(value)
  return Number.isSafeInteger(count) && count >= 0 ? count : undefined
}

export function useToolboxApplications({
  backendStatus,
  sendCommand,
  waitForIpcEvent,
}: UseToolboxApplicationsOptions) {
  const [toolboxTools, setToolboxTools] = useState<ToolRuntimeState[]>(() =>
    createInitialToolboxRuntimeState()
  )
  const toolboxToolsRef = useRef(toolboxTools)
  const [toolboxSyncing, setToolboxSyncing] = useState(false)
  const [toolboxSyncedAt, setToolboxSyncedAt] = useState<number | null>(null)
  const [mainSystemSizeBytes, setMainSystemSizeBytes] = useState<number | undefined>()
  const [mainSystemFileCount, setMainSystemFileCount] = useState<number | undefined>()
  const [dependencySizeBytes, setDependencySizeBytes] = useState<number | undefined>()
  const [dependencyFileCount, setDependencyFileCount] = useState<number | undefined>()
  const [sharedLayerSizeBytes, setSharedLayerSizeBytes] = useState<number | undefined>()
  const [sharedLayerFileCount, setSharedLayerFileCount] = useState<number | undefined>()
  const [workspaceSizeBytes, setWorkspaceSizeBytes] = useState<number | undefined>()
  const [workspaceFileCount, setWorkspaceFileCount] = useState<number | undefined>()
  const refreshPromiseRef = useRef<Promise<void> | null>(null)
  const actionRevisionRef = useRef(0)

  useEffect(() => {
    toolboxToolsRef.current = toolboxTools
  }, [toolboxTools])

  const mergeLocalProjectSizes = useCallback(
    async (
      tools: ToolRuntimeState[],
      forceRefresh = false
    ): Promise<ToolRuntimeState[]> => {
      const api = window.electron
      if (!api?.invoke) return tools
      try {
        const payload = (await api.invoke(
          'app:get-platform-tool-sizes',
          { forceRefresh }
        )) as PlatformToolSizesPayload
        if (payload?.ok === false) return tools
        const mainSystemBytes = Number(payload?.main_system?.project_size_bytes)
        if (Number.isFinite(mainSystemBytes) && mainSystemBytes >= 0) {
          setMainSystemSizeBytes(mainSystemBytes)
        }
        setMainSystemFileCount(normalizeInventoryCount(payload?.main_system?.file_count))
        const dependencyBytes = Number(payload?.main_system?.dependency_size_bytes)
        if (Number.isFinite(dependencyBytes) && dependencyBytes >= 0) {
          setDependencySizeBytes(dependencyBytes)
        }
        setDependencyFileCount(
          normalizeInventoryCount(payload?.main_system?.dependency_file_count)
        )
        const sharedLayerBytes = Number(payload?.shared_layer?.project_size_bytes)
        if (Number.isFinite(sharedLayerBytes) && sharedLayerBytes >= 0) {
          setSharedLayerSizeBytes(sharedLayerBytes)
        }
        setSharedLayerFileCount(
          normalizeInventoryCount(payload?.shared_layer?.file_count)
        )
        const workspaceBytes = Number(payload?.workspace?.project_size_bytes)
        if (Number.isFinite(workspaceBytes) && workspaceBytes >= 0) {
          setWorkspaceSizeBytes(workspaceBytes)
        }
        setWorkspaceFileCount(normalizeInventoryCount(payload?.workspace?.file_count))
        const localCatalog = hydrateToolboxRuntimeStateFromBackend(payload?.tools)
        const discovered = tools.length ? tools : localCatalog
        return mergeToolboxProjectSizes(discovered, payload?.tools)
      } catch {
        return tools
      }
    },
    []
  )

  const refreshToolboxTools = useCallback(async () => {
    if (refreshPromiseRef.current) return refreshPromiseRef.current

    const refreshRevision = actionRevisionRef.current
    const refreshPromise = (async () => {
      setToolboxSyncing(true)
      try {
        let next = toolboxToolsRef.current
        const sent = sendCommand('toolbox_list_tools', {
          source: 'app_toolbox_sync',
        })
        if (sent.ok) {
          const payload = await waitForIpcEvent(
            'toolbox_list_tools_result',
            TOOLBOX_LIST_TIMEOUT_MS
          )
          if (payload.ok !== false) {
            const hydrated = hydrateToolboxRuntimeStateFromBackend(payload.tools)
            next = hydrated.length ? hydrated : createInitialToolboxRuntimeState()
          }
        }
        const withSizes = await mergeLocalProjectSizes(next, true)
        if (refreshRevision !== actionRevisionRef.current) return
        setToolboxTools(withSizes)
        setToolboxSyncedAt(Date.now())
      } catch {
        // Backend metadata can be unavailable while the trusted local
        // inventory remains readable; keep capacity refresh independent.
        const withSizes = await mergeLocalProjectSizes(
          toolboxToolsRef.current,
          true
        )
        if (refreshRevision !== actionRevisionRef.current) return
        setToolboxTools(withSizes)
        setToolboxSyncedAt(Date.now())
      } finally {
        setToolboxSyncing(false)
        refreshPromiseRef.current = null
      }
    })()

    refreshPromiseRef.current = refreshPromise
    return refreshPromise
  }, [mergeLocalProjectSizes, sendCommand, waitForIpcEvent])

  useEffect(() => {
    if (backendStatus === 'Connected') void refreshToolboxTools()
  }, [backendStatus, refreshToolboxTools])

  useEffect(() => {
    let disposed = false
    let attempts = 0
    let retryTimer: number | null = null

    const hydrateLocalSizes = async () => {
      attempts += 1
      const withSizes = await mergeLocalProjectSizes(toolboxToolsRef.current)
      if (disposed) return
      const sizesById = new Map(withSizes.map((tool) => [tool.id, tool]))
      setToolboxTools((current) =>
        current.length
          ? current.map((tool) => {
              const sized = sizesById.get(tool.id)
              if (!sized) return tool
              return {
                ...tool,
                folderPath: sized.folderPath,
                manifestPath: sized.manifestPath,
                codePath: sized.codePath,
                projectSizeBytes: sized.projectSizeBytes,
                projectFileCount: sized.projectFileCount,
                capacityBreakdown: sized.capacityBreakdown,
              }
            })
          : withSizes
      )
      setToolboxSyncedAt((current) => current ?? Date.now())
      if (
        attempts < LOCAL_TOOL_SIZE_RETRY_LIMIT &&
        hasMissingProjectSizes(withSizes)
      ) {
        retryTimer = window.setTimeout(hydrateLocalSizes, LOCAL_TOOL_SIZE_RETRY_MS)
      }
    }

    void hydrateLocalSizes()
    return () => {
      disposed = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
    }
  }, [mergeLocalProjectSizes])

  useEffect(() => {
    const reload = () => void refreshToolboxTools()
    window.addEventListener('gptbridge:global-data-reload', reload)
    return () => window.removeEventListener('gptbridge:global-data-reload', reload)
  }, [refreshToolboxTools])

  const executeToolboxAction = useCallback(
    async (toolId: string, action: ToolAction) => {
      const target = toolboxToolsRef.current.find((tool) => tool.id === toolId)
      if (target?.launchable === false) {
        setToolboxTools((previous) =>
          previous.map((tool) =>
            tool.id === toolId
              ? { ...tool, status: 'stopped', updatedAt: Date.now(), note: t.disabled }
              : tool
          )
        )
        return
      }

      actionRevisionRef.current += 1
      setToolboxTools((previous) =>
        resolveToolboxToolAction(previous, toolId, action, 'pending')
      )

      const command = action === 'start' ? 'toolbox_start_tool' : 'toolbox_force_close_tool'
      const resultEvent = `${command}_result`
      const failMessage =
        action === 'start'
          ? t.startFailed
          : t.stopFailed
      const requestId = `${toolId}:${action}:${Date.now()}:${Math.random().toString(16).slice(2)}`

      try {
        const waitResult = waitForIpcEvent(
          resultEvent,
          TOOLBOX_ACTION_TIMEOUT_MS,
          (payload) => String(payload.request_id || '') === requestId
        )
        const sent = sendCommand(command, { tool_id: toolId, request_id: requestId })
        if (!sent.ok) throw new Error(sent.message || failMessage)
        const result = await waitResult
        if (result.ok === false) throw new Error(String(result.message || failMessage))
        setToolboxTools((previous) =>
          resolveToolboxToolAction(previous, toolId, action, 'settled')
        )
        const staleRefresh = refreshPromiseRef.current
        if (staleRefresh) await staleRefresh
        await refreshToolboxTools()
      } catch (error) {
        const rawMessage = error instanceof Error ? error.message : failMessage
        const message = rawMessage === 'PERMISSION_DENIED'
          ? mainSystemLocale['errors.permission_denied']
          : failMessage
        setToolboxTools((previous) =>
          previous.map((tool) =>
            tool.id === toolId
              ? { ...tool, status: 'error', updatedAt: Date.now(), note: message }
              : tool
          )
        )
      }
    },
    [refreshToolboxTools, sendCommand, waitForIpcEvent]
  )

  const handleToolboxAction = useCallback(
    (toolId: string, action: ToolAction) => void executeToolboxAction(toolId, action),
    [executeToolboxAction]
  )

  return {
    toolboxTools,
    toolboxSyncing,
    toolboxSyncedAt,
    mainSystemSizeBytes,
    mainSystemFileCount,
    dependencySizeBytes,
    dependencyFileCount,
    sharedLayerSizeBytes,
    sharedLayerFileCount,
    workspaceSizeBytes,
    workspaceFileCount,
    refreshToolboxTools,
    handleToolboxAction,
  }
}
