import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  ToolAction,
  ToolRuntimeState,
} from '@/ui/developer-mode/tools/types'
import {
  createInitialToolboxRuntimeState,
  hydrateToolboxRuntimeStateFromBackend,
  mergeToolboxProjectSizes,
  resolveToolboxToolAction,
} from '@/ui/toolbox/tools/runtimeState'

type SendCommandResult = {
  ok: boolean
  queued: boolean
  message?: string
}

type SendCommand = (
  command: string,
  payload?: unknown
) => SendCommandResult

type WaitForIpcEvent = (
  eventName: string,
  timeoutMs: number
) => Promise<Record<string, unknown>>

type UseToolboxApplicationsOptions = {
  activeView: string
  backendStatus: string
  sendCommand: SendCommand
  waitForIpcEvent: WaitForIpcEvent
}

type PlatformToolSizesPayload = {
  ok?: boolean
  tools?: unknown
}

const TOOLBOX_LIST_TIMEOUT_MS = 20000
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

export function useToolboxApplications({
  activeView,
  backendStatus,
  sendCommand,
  waitForIpcEvent,
}: UseToolboxApplicationsOptions) {
  const [toolboxTools, setToolboxTools] = useState<ToolRuntimeState[]>(() =>
    createInitialToolboxRuntimeState()
  )
  const toolboxToolsRef = useRef<ToolRuntimeState[]>(toolboxTools)
  const [toolboxSyncing, setToolboxSyncing] = useState(false)
  const [toolboxSyncedAt, setToolboxSyncedAt] = useState<number | null>(null)
  const toolboxRefreshPromiseRef = useRef<Promise<void> | null>(null)

  useEffect(() => {
    toolboxToolsRef.current = toolboxTools
  }, [toolboxTools])

  const mergeLocalProjectSizes = useCallback(
    async (tools: ToolRuntimeState[]): Promise<ToolRuntimeState[]> => {
      const api = window.electron
      if (!api?.invoke) return tools

      try {
        const payload = (await api.invoke(
          'app:get-platform-tool-sizes'
        )) as PlatformToolSizesPayload
        if (payload?.ok === false) return tools
        return mergeToolboxProjectSizes(tools, payload?.tools)
      } catch {
        return tools
      }
    },
    []
  )

  const refreshToolboxTools = useCallback(async () => {
    if (toolboxRefreshPromiseRef.current) {
      return toolboxRefreshPromiseRef.current
    }

    const refreshPromise = (async () => {
      setToolboxSyncing(true)

      try {
        const result = sendCommand('toolbox_list_tools', {
          source: 'app_toolbox_sync',
        })
        if (!result.ok && !result.queued) return
        const payload = await waitForIpcEvent(
          'toolbox_list_tools_result',
          TOOLBOX_LIST_TIMEOUT_MS
        )
        if (payload.ok === false) return
        const hydratedTools = hydrateToolboxRuntimeStateFromBackend(payload.tools)
        const nextTools =
          hydratedTools.length > 0
            ? hydratedTools
            : createInitialToolboxRuntimeState()
        const toolsWithLocalSizes = await mergeLocalProjectSizes(nextTools)
        setToolboxTools(toolsWithLocalSizes)
        setToolboxSyncedAt(Date.now())
      } catch {
        // Keep current toolbox state on transient refresh failure.
      } finally {
        setToolboxSyncing(false)
        toolboxRefreshPromiseRef.current = null
      }
    })()

    toolboxRefreshPromiseRef.current = refreshPromise
    return refreshPromise
  }, [mergeLocalProjectSizes, sendCommand, waitForIpcEvent])

  useEffect(() => {
    if (backendStatus !== 'Connected') return
    void refreshToolboxTools()
  }, [backendStatus, refreshToolboxTools])

  useEffect(() => {
    let disposed = false
    let attempts = 0
    let retryTimer: number | null = null

    const hydrateLocalSizes = async () => {
      attempts += 1
      const currentTools = toolboxToolsRef.current
      const toolsWithLocalSizes = await mergeLocalProjectSizes(currentTools)
      if (disposed) return
      setToolboxTools(toolsWithLocalSizes)
      setToolboxSyncedAt((current) => current ?? Date.now())

      if (
        attempts < LOCAL_TOOL_SIZE_RETRY_LIMIT &&
        hasMissingProjectSizes(toolsWithLocalSizes)
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
    const handler = () => {
      void refreshToolboxTools()
    }

    window.addEventListener('gptbridge:global-data-reload', handler)
    return () => window.removeEventListener('gptbridge:global-data-reload', handler)
  }, [refreshToolboxTools])

  useEffect(() => {
    if (activeView !== 'toolbox' && activeView !== 'developer') return
    void refreshToolboxTools()
  }, [activeView, refreshToolboxTools])

  useEffect(() => {
    const reloadEvents = new Set([
      'toolbox_add_tool_result',
      'toolbox_start_tool_result',
      'toolbox_stop_tool_result',
      'toolbox_save_tool_code_result',
    ])

    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = (customEvent.detail || {}) as Record<string, unknown>
      const eventName = String(detail.event || '')
      if (!reloadEvents.has(eventName)) return

      const payload = (detail.payload || {}) as Record<string, unknown>
      if (payload.ok === false) return
      void refreshToolboxTools()
    }

    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [refreshToolboxTools])

  const executeToolboxAction = useCallback(
    async (toolId: string, action: ToolAction) => {
      setToolboxTools((prev) =>
        resolveToolboxToolAction(prev, toolId, action, 'pending')
      )

      const command =
        action === 'start' ? 'toolbox_start_tool' : 'toolbox_stop_tool'
      const resultEvent =
        action === 'start'
          ? 'toolbox_start_tool_result'
          : 'toolbox_stop_tool_result'
      const failMessage =
        action === 'start'
          ? '工具啟動失敗，請稍後再試。'
          : '工具停止失敗，請稍後再試。'

      sendCommand(command, { tool_id: toolId })

      try {
        const result = await waitForIpcEvent(resultEvent, 10000)
        if (result.ok === false) {
          const message = String(result.message || failMessage)
          setToolboxTools((prev) =>
            prev.map((tool) =>
              tool.id === toolId
                ? {
                    ...tool,
                    status: 'error',
                    updatedAt: Date.now(),
                    note: message,
                  }
                : tool
            )
          )
          return
        }
        await refreshToolboxTools()
      } catch {
        setToolboxTools((prev) =>
          prev.map((tool) =>
            tool.id === toolId
              ? {
                  ...tool,
                  status: 'error',
                  updatedAt: Date.now(),
                  note: failMessage,
                }
              : tool
          )
        )
      }
    },
    [refreshToolboxTools, sendCommand, waitForIpcEvent]
  )

  const handleToolboxAction = useCallback(
    (toolId: string, action: ToolAction) => {
      void executeToolboxAction(toolId, action)
    },
    [executeToolboxAction]
  )

  return {
    toolboxTools,
    toolboxSyncing,
    toolboxSyncedAt,
    handleToolboxAction,
  }
}
