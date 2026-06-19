import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'

export interface ToolRunResult {
  ok?: boolean
  message?: string
  tool_id?: string
  request_id?: string
  stdout?: string
  stderr?: string
  stdout_encoding?: string
  stderr_encoding?: string
  exit_code?: number
  cancelled?: boolean
}

export interface OpenPathResult {
  ok?: boolean
  message?: string
}

type SendCommandResult = {
  ok: boolean
  queued: boolean
  message?: string
}

function isStandaloneToolWindow(): boolean {
  return Boolean((window as any).gptBridge?.standaloneTool)
}

function useLocalBackendSocket() {
  const [status, setStatus] = useState('Disconnected')
  const socketRef = useRef<WebSocket | null>(null)
  const queueRef = useRef<Array<{ command: string; payload: unknown }>>([])
  const reconnectTimerRef = useRef<number | null>(null)

  const sendCommand = useCallback(
    (command: string, payload: unknown = {}): SendCommandResult => {
      const socket = socketRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        queueRef.current.push({ command, payload })
        if (queueRef.current.length > 100) {
          queueRef.current.splice(0, queueRef.current.length - 100)
        }
        return {
          ok: false,
          queued: true,
          message: 'WebSocket is not connected, command queued',
        }
      }

      socket.send(JSON.stringify({ command, payload }))
      return { ok: true, queued: false }
    },
    []
  )

  useEffect(() => {
    let disposed = false

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current === null) return
      window.clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }

    const ensureBackendStarted = async () => {
      const api = (window as any).electron
      if (!api?.invoke) return
      try {
        await api.invoke('app:ensure-backend-started')
      } catch {
        // Tool windows can still connect if the backend is already running.
      }
    }

    const scheduleReconnect = () => {
      if (disposed || reconnectTimerRef.current !== null) return
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void connect()
      }, 1500)
    }

    const connect = async () => {
      if (disposed) return
      const current = socketRef.current
      if (
        current &&
        (current.readyState === WebSocket.OPEN ||
          current.readyState === WebSocket.CONNECTING)
      ) {
        return
      }

      setStatus('Connecting')
      await ensureBackendStarted()
      if (disposed) return

      const socket = new WebSocket('ws://127.0.0.1:8765')
      socketRef.current = socket

      socket.onopen = () => {
        setStatus('Connected')
        clearReconnectTimer()
        const queued = queueRef.current.splice(0)
        for (const item of queued) {
          socket.send(JSON.stringify({ command: item.command, payload: item.payload }))
        }
        window.dispatchEvent(
          new CustomEvent('socket_connected', { detail: { connected: true } })
        )
      }

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(String(event.data)) as Record<string, unknown>
          if (payload.event && typeof payload.event === 'string') {
            window.dispatchEvent(
              new CustomEvent('ipc_event', {
                detail: {
                  event: payload.event,
                  payload: payload.payload,
                },
              })
            )
          }
        } catch {
          // Ignore malformed backend frames in tool windows.
        }
      }

      socket.onerror = () => {
        setStatus('Error')
      }

      socket.onclose = () => {
        if (socketRef.current === socket) socketRef.current = null
        setStatus('Disconnected')
        window.dispatchEvent(
          new CustomEvent('socket_connected', { detail: { connected: false } })
        )
        scheduleReconnect()
      }
    }

    void connect()

    return () => {
      disposed = true
      clearReconnectTimer()
      const socket = socketRef.current
      socketRef.current = null
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close()
      }
    }
  }, [])

  return { sendCommand, status }
}

export function waitForIpcEvent<T = Record<string, unknown>>(
  eventName: string,
  timeoutMs: number,
  predicate?: (payload: Record<string, unknown>) => boolean
): Promise<T> {
  return new Promise((resolve, reject) => {
    let timer = 0
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== eventName) return
      const payload = (detail.payload || {}) as Record<string, unknown>
      if (predicate && !predicate(payload)) return
      window.clearTimeout(timer)
      window.removeEventListener('ipc_event', handler)
      resolve(payload as T)
    }

    timer = window.setTimeout(() => {
      window.removeEventListener('ipc_event', handler)
      reject(new Error(`等待 ${eventName} 逾時`))
    }, timeoutMs)
    window.addEventListener('ipc_event', handler)
  })
}

export function parseToolJson<T = Record<string, unknown>>(
  stdout: string | undefined
): T | null {
  const text = String(stdout || '').trim()
  if (!text) return null
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? (parsed as T) : null
  } catch {
    return null
  }
}

export function formatRunOutput(result: ToolRunResult | null): string {
  if (!result) return ''
  const parts = [
    result.stdout ? `輸出\n${result.stdout.trim()}` : '',
    result.stderr ? `錯誤\n${result.stderr.trim()}` : '',
  ].filter(Boolean)
  if (parts.length > 0) return parts.join('\n\n')
  return result.message || '工具已完成，沒有額外輸出。'
}

export function formatFileSize(size: number | null | undefined): string {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`
}

export function isAbsoluteFilesystemPath(value: string): boolean {
  return /^[a-zA-Z]:[\\/]/.test(value) || /^\\\\/.test(value) || value.startsWith('/')
}

export async function selectFolder(): Promise<string> {
  if (window.gptBridge?.selectFolder) return await window.gptBridge.selectFolder()
  return String((await (window as any).electron?.invoke?.('dialog:select-folder')) || '')
}

export async function openFile(): Promise<string> {
  if (window.gptBridge?.openFile) return await window.gptBridge.openFile()
  return String((await (window as any).electron?.invoke?.('dialog:open-file')) || '')
}

export async function openPath(payload: Record<string, unknown>): Promise<OpenPathResult> {
  if (window.gptBridge?.openPath) return await window.gptBridge.openPath(payload)
  return ((await (window as any).electron?.invoke?.('app:open-path', payload)) || {
    ok: false,
    message: '目前環境不支援開啟路徑',
  }) as OpenPathResult
}

export function useToolRunner(toolId: string, timeoutMs = 120000) {
  const { sendCommand, status: socketStatus } = useLocalBackendSocket()
  const queueRef = useRef<Promise<void>>(Promise.resolve())

  useEffect(() => {
    if (isStandaloneToolWindow()) return

    sendCommand('toolbox_start_tool', {
      tool_id: toolId,
      source: 'tool_window',
    })

    const stopTool = () => {
      sendCommand('toolbox_stop_tool', {
        tool_id: toolId,
        source: 'tool_window',
      })
    }

    window.addEventListener('beforeunload', stopTool)
    return () => {
      window.removeEventListener('beforeunload', stopTool)
      stopTool()
    }
  }, [sendCommand, toolId])

  const requestToolRun = useCallback(
    async (args: string[]) => {
      const runRequest = async (): Promise<ToolRunResult> => {
        const requestId = `${toolId}:${Date.now()}:${Math.random()
          .toString(16)
          .slice(2)}`
        const resultPromise = waitForIpcEvent<ToolRunResult>(
          'toolbox_run_tool_result',
          timeoutMs,
          (payload) =>
            String(payload.tool_id || '') === toolId &&
            String(payload.request_id || '') === requestId
        )
        const sent = sendCommand('toolbox_run_tool', {
          tool_id: toolId,
          args,
          source: 'tool_window',
          request_id: requestId,
        })
        if (!sent.ok && !sent.queued) {
          return {
            ok: false,
            tool_id: toolId,
            request_id: requestId,
            message: sent.message || '後端尚未接收工具指令',
          }
        }
        return await resultPromise
      }

      const queued = queueRef.current.then(runRequest, runRequest)
      queueRef.current = queued.then(
        () => undefined,
        () => undefined
      )
      return queued
    },
    [sendCommand, timeoutMs, toolId]
  )

  const cancelToolRun = useCallback(async () => {
    const resultPromise = waitForIpcEvent<OpenPathResult>(
      'toolbox_cancel_tool_run_result',
      10000,
      (payload) => !payload.tool_id || String(payload.tool_id) === toolId
    )
    const sent = sendCommand('toolbox_cancel_tool_run', {
      tool_id: toolId,
      source: 'tool_window',
    })
    if (!sent.ok && !sent.queued) {
      return { ok: false, message: sent.message || '後端尚未接收停止指令' }
    }
    return await resultPromise
  }, [sendCommand, toolId])

  return {
    cancelToolRun,
    requestToolRun,
    sendCommand,
    socketStatus,
  }
}

export const toolWindowStyles: Record<string, CSSProperties> = {
  app: {
    minHeight: '100vh',
    background: '#0b0f17',
    color: '#f8fafc',
    fontFamily: '"Noto Sans TC", "Segoe UI", sans-serif',
    padding: '28px',
    boxSizing: 'border-box',
  },
  card: {
    maxWidth: '980px',
    margin: '0 auto',
    background: '#111827',
    border: '1px solid #243044',
    borderRadius: '10px',
    padding: '22px',
    boxShadow: '0 22px 54px rgba(0, 0, 0, 0.42)',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '16px',
    alignItems: 'flex-start',
    marginBottom: '22px',
  },
  kicker: {
    color: '#7dd3fc',
    fontSize: '12px',
    fontWeight: 800,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    marginBottom: '6px',
  },
  title: {
    margin: 0,
    fontSize: '24px',
    lineHeight: 1.25,
  },
  muted: {
    margin: '8px 0 0',
    color: '#94a3b8',
    fontSize: '14px',
    lineHeight: 1.6,
  },
  badge: {
    border: '1px solid #334155',
    borderRadius: '999px',
    color: '#cbd5e1',
    padding: '5px 10px',
    fontSize: '12px',
    fontWeight: 700,
  },
  fieldGroup: {
    marginBottom: '16px',
  },
  label: {
    display: 'block',
    color: '#cbd5e1',
    fontSize: '13px',
    fontWeight: 700,
    marginBottom: '8px',
  },
  inlineRow: {
    display: 'flex',
    gap: '10px',
  },
  input: {
    flex: 1,
    minWidth: 0,
    background: '#0b1220',
    border: '1px solid #334155',
    borderRadius: '8px',
    color: '#f8fafc',
    padding: '11px 12px',
    fontSize: '14px',
    outline: 'none',
  },
  textarea: {
    width: '100%',
    minHeight: '76px',
    boxSizing: 'border-box',
    background: '#0b1220',
    border: '1px solid #334155',
    borderRadius: '8px',
    color: '#f8fafc',
    padding: '11px 12px',
    fontSize: '14px',
    outline: 'none',
    resize: 'vertical',
  },
  notice: {
    background: '#0b1220',
    border: '1px solid #1e293b',
    borderRadius: '8px',
    color: '#cbd5e1',
    padding: '12px',
    lineHeight: 1.6,
    marginBottom: '16px',
  },
  checkboxRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '9px',
    color: '#e2e8f0',
    cursor: 'pointer',
  },
  noticeText: {
    color: '#94a3b8',
    fontSize: '12px',
    lineHeight: 1.6,
    margin: '8px 0 0',
  },
  actions: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '10px',
    justifyContent: 'flex-end',
    marginTop: '18px',
  },
  primaryButton: {
    border: 0,
    borderRadius: '8px',
    background: '#7dd3fc',
    color: '#082f49',
    padding: '11px 16px',
    fontWeight: 900,
    cursor: 'pointer',
  },
  secondaryButton: {
    border: '1px solid #334155',
    borderRadius: '8px',
    background: '#172033',
    color: '#e2e8f0',
    padding: '10px 13px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  dangerButton: {
    border: '1px solid #b91c1c',
    borderRadius: '8px',
    background: '#7f1d1d',
    color: '#fee2e2',
    padding: '10px 13px',
    fontWeight: 900,
    cursor: 'pointer',
  },
  statusPanel: {
    marginTop: '18px',
    border: '1px solid #243044',
    borderRadius: '8px',
    background: '#0b1220',
    padding: '14px',
  },
  statusLine: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    marginBottom: '10px',
  },
  statusDot: {
    width: '10px',
    height: '10px',
    borderRadius: '999px',
  },
  output: {
    maxHeight: '260px',
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    margin: 0,
    color: '#cbd5e1',
    fontSize: '12px',
    lineHeight: 1.5,
  },
  resultPanel: {
    border: '1px solid #243044',
    borderRadius: '8px',
    background: '#0b1220',
    padding: '14px',
    marginTop: '14px',
  },
  resultList: {
    display: 'grid',
    gap: '8px',
    marginTop: '10px',
    maxHeight: '360px',
    overflow: 'auto',
  },
  resultRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    border: '1px solid #1e293b',
    borderRadius: '8px',
    background: '#111827',
    padding: '10px',
  },
  resultText: {
    flex: 1,
    minWidth: 0,
  },
  resultPath: {
    display: 'block',
    color: '#f8fafc',
    fontWeight: 800,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  resultMeta: {
    display: 'block',
    color: '#94a3b8',
    fontSize: '12px',
    lineHeight: 1.5,
  },
  sliderGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))',
    gap: '12px',
    marginTop: '14px',
  },
  sliderControl: {
    display: 'grid',
    gap: '7px',
    background: '#08101d',
    border: '1px solid #243044',
    borderRadius: '8px',
    padding: '10px',
  },
  sliderHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '10px',
    color: '#e2e8f0',
    fontSize: '12px',
    fontWeight: 800,
  },
  rangeInput: {
    width: '100%',
    minWidth: 0,
  },
  sliderMeta: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '10px',
    color: '#94a3b8',
    fontSize: '11px',
  },
}
