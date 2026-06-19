import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BootLogger } from '../BootLogger'
import { eventBus } from '../RuntimeEventBus'
import type { AuditCheckItem } from '../types/health'

type TaskProgressEntry = {
  stage: string
  status: string
  message: string
  percent: number
}

type LogGroups = {
  core: string[]
  design: string[]
  developer: string[]
}

type BackendSocketState = {
  status: string
  chatgptStatus: string
  geminiStatus: string
  auditChecks: AuditCheckItem[]
  config: Record<string, unknown>
  lastStatusAt: number | null
  lastAuditAt: number | null
  lastError: string
  recentEvents: string[]
  chatgptAnswers: string[]
  geminiAnswers: string[]
  logGroups: LogGroups
  designDiff: string
  childFile: string
  taskProgress: {
    design: TaskProgressEntry
    rescue: TaskProgressEntry
    developer: TaskProgressEntry
  }
  auditRunning: boolean
  backupRecords: Array<Record<string, unknown>>
  currentAccounts: Array<Record<string, unknown>>
  aiCostProfiles: Array<Record<string, unknown>>
}

type SendCommandResult = {
  ok: boolean
  queued: boolean
  message?: string
}

type BackendConnectionSnapshot = {
  status: string
  connected: boolean
  updatedAt: number | null
}

const EMPTY_PROGRESS: TaskProgressEntry = {
  stage: 'idle',
  status: 'idle',
  message: '',
  percent: 0,
}

const INITIAL_STATE: BackendSocketState = {
  status: 'Disconnected',
  chatgptStatus: 'UNKNOWN',
  geminiStatus: 'UNKNOWN',
  auditChecks: [],
  config: {},
  lastStatusAt: null,
  lastAuditAt: null,
  lastError: '',
  recentEvents: [],
  chatgptAnswers: [],
  geminiAnswers: [],
  logGroups: {
    core: [],
    design: [],
    developer: [],
  },
  designDiff: '',
  childFile: '',
  taskProgress: {
    design: EMPTY_PROGRESS,
    rescue: EMPTY_PROGRESS,
    developer: EMPTY_PROGRESS,
  },
  auditRunning: false,
  backupRecords: [],
  currentAccounts: [],
  aiCostProfiles: [],
}

const WS_RECONNECT_BASE_DELAY_MS = 1200
const WS_RECONNECT_MAX_DELAY_MS = 6000

const openBackendSockets = new Set<WebSocket>()

let backendConnectionSnapshot: BackendConnectionSnapshot = {
  status: INITIAL_STATE.status,
  connected: false,
  updatedAt: null,
}

function updateBackendConnectionSnapshot(
  status: string,
  socket?: WebSocket,
  connected?: boolean
): BackendConnectionSnapshot {
  if (socket && connected === true) {
    openBackendSockets.add(socket)
  } else if (socket && connected === false) {
    openBackendSockets.delete(socket)
  }

  const hasOpenSocket = openBackendSockets.size > 0
  backendConnectionSnapshot = {
    status: hasOpenSocket ? 'Connected' : status,
    connected: hasOpenSocket,
    updatedAt: Date.now(),
  }

  return backendConnectionSnapshot
}

export function getBackendConnectionSnapshot(): BackendConnectionSnapshot {
  return { ...backendConnectionSnapshot }
}

function normalizeEvent(text: string): string {
  return text.length > 160 ? `${text.slice(0, 157)}...` : text
}

export const useBackendSocket = () => {
  const [state, setState] = useState<BackendSocketState>(INITIAL_STATE)
  const [lastError, setLastError] = useState<string | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const pendingCommandsRef = useRef<Array<{ command: string; payload: unknown }>>([])
  const reconnectTimerRef = useRef<number | null>(null)
  const reconnectAttemptRef = useRef(0)

  const sendCommand = useCallback(
    (command: string, payload: unknown = {}): SendCommandResult => {
      if (!socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) {
        const queued = pendingCommandsRef.current
        queued.push({ command, payload })
        if (queued.length > 100) queued.splice(0, queued.length - 100)
        const errorMsg = 'WebSocket is not connected, command queued'
        setLastError(errorMsg)
        BootLogger.log('WebSocket', 'SEND_QUEUE', { command, queueSize: queued.length }, 'warn')
        return { ok: false, queued: true, message: errorMsg }
      }

      socketRef.current.send(JSON.stringify({ command, payload }))
      BootLogger.log('WebSocket', 'SEND', { command })
      return { ok: true, queued: false }
    },
    []
  )

  const clearLog = useCallback(() => {
    setState((prev) => ({
      ...prev,
      logGroups: {
        core: [],
        design: [],
        developer: [],
      },
      recentEvents: [],
    }))
  }, [])

  useEffect(() => {
    const wsUrl = 'ws://127.0.0.1:8765'
    let disposed = false
    let ensureStartPromise: Promise<void> | null = null

    const clearReconnectTimer = () => {
      const timer = reconnectTimerRef.current
      if (timer !== null) {
        window.clearTimeout(timer)
        reconnectTimerRef.current = null
      }
    }

    const scheduleReconnect = () => {
      if (disposed) return
      clearReconnectTimer()
      const nextAttempt = reconnectAttemptRef.current + 1
      reconnectAttemptRef.current = nextAttempt
      const delay = Math.min(
        WS_RECONNECT_MAX_DELAY_MS,
        WS_RECONNECT_BASE_DELAY_MS * nextAttempt
      )
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void connect()
      }, delay)
      BootLogger.log('WebSocket', 'RECONNECT_SCHEDULED', {
        attempt: nextAttempt,
        delay,
      })
    }

    const ensureBackendStarted = async () => {
      if (ensureStartPromise) return ensureStartPromise
      ensureStartPromise = (async () => {
        const api = (window as any).electron
        if (!api?.invoke) return
        try {
          await api.invoke('app:ensure-backend-started')
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          BootLogger.log('WebSocket', 'ENSURE_BACKEND_FAILED', { error: message }, 'warn')
        }
      })()
      await ensureStartPromise
      ensureStartPromise = null
    }

    const connect = async () => {
      if (disposed) return
      if (
        socketRef.current &&
        (socketRef.current.readyState === WebSocket.OPEN ||
          socketRef.current.readyState === WebSocket.CONNECTING)
      ) {
        return
      }

      setState((prev) => ({
        ...prev,
        status: 'Connecting',
      }))
      updateBackendConnectionSnapshot('Connecting')
      BootLogger.log('WebSocket', 'CONNECTING', { url: wsUrl })
      await ensureBackendStarted()
      if (disposed) return

      const socket = new WebSocket(wsUrl)
      socketRef.current = socket

      socket.onopen = () => {
        reconnectAttemptRef.current = 0
        clearReconnectTimer()
        setLastError(null)
        updateBackendConnectionSnapshot('Connected', socket, true)
        setState((prev) => ({
          ...prev,
          status: 'Connected',
          lastStatusAt: Date.now(),
        }))
        BootLogger.log('WebSocket', 'OPEN', { url: wsUrl })
        const queued = pendingCommandsRef.current.splice(0)
        for (const item of queued) {
          socket.send(JSON.stringify({ command: item.command, payload: item.payload }))
          BootLogger.log('WebSocket', 'SEND_FLUSH', { command: item.command })
        }
        eventBus.emit('socket_connected', { connected: true })
      }

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(String(event.data)) as Record<string, unknown>

          if (payload.event && typeof payload.event === 'string') {
            eventBus.emit(payload.event, payload.payload)
            window.dispatchEvent(
              new CustomEvent('ipc_event', {
                detail: {
                  event: payload.event,
                  payload: payload.payload,
                },
              })
            )
          }

          const messageText = normalizeEvent(JSON.stringify(payload))

          setState((prev) => ({
            ...prev,
            lastStatusAt: Date.now(),
            recentEvents: [...prev.recentEvents.slice(-39), messageText],
            logGroups: {
              ...prev.logGroups,
              core: [...prev.logGroups.core.slice(-199), messageText],
            },
          }))

          eventBus.emit('backend_message', payload)
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          BootLogger.log('WebSocket', 'PARSE_ERROR', { error: message }, 'error')
        }
      }

      socket.onerror = () => {
        const errorMsg = 'WebSocket error'
        setLastError(errorMsg)
        updateBackendConnectionSnapshot('Error')
        setState((prev) => ({
          ...prev,
          status: 'Error',
          lastError: errorMsg,
        }))
        BootLogger.log('WebSocket', 'ERROR', {}, 'error')
      }

      socket.onclose = () => {
        if (socketRef.current === socket) {
          socketRef.current = null
        }
        const snapshot = updateBackendConnectionSnapshot('Disconnected', socket, false)
        setState((prev) => ({
          ...prev,
          status: 'Disconnected',
        }))
        BootLogger.log('WebSocket', 'CLOSED')
        eventBus.emit('socket_connected', { connected: snapshot.connected })
        if (!disposed) {
          scheduleReconnect()
        }
      }
    }

    void connect()

    return () => {
      disposed = true
      clearReconnectTimer()
      reconnectAttemptRef.current = 0
      const socket = socketRef.current
      socketRef.current = null
      if (socket) {
        updateBackendConnectionSnapshot('Disconnected', socket, false)
      }
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close()
      }
    }
  }, [])

  return useMemo(
    () => ({
      ...state,
      sendCommand,
      clearLog,
      lastError: lastError ?? state.lastError,
    }),
    [state, sendCommand, clearLog, lastError]
  )
}
