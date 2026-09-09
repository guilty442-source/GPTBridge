import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BootLogger } from '../BootLogger'
import { eventBus } from '../RuntimeEventBus'
import { getAuthenticatedBackendWebSocketUrl } from '../services/backendSession'
type BackendSocketState = {
  status: string
  lastStatusAt: number | null
  lastError: string
  reconnectAttempt: number
  queuedCommands: number
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

const INITIAL_STATE: BackendSocketState = {
  status: 'Disconnected',
  lastStatusAt: null,
  lastError: '',
  reconnectAttempt: 0,
  queuedCommands: 0,
}

const WS_RECONNECT_BASE_DELAY_MS = 500
const WS_RECONNECT_MAX_DELAY_MS = 8000
const WS_COMMAND_QUEUE_MAX = 50
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

export const useBackendSocket = () => {
  const [state, setState] = useState<BackendSocketState>(INITIAL_STATE)
  const [lastError, setLastError] = useState<string | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const reconnectAttemptRef = useRef(0)
  const commandQueueRef = useRef<Array<{ command: string; payload: unknown }>>([])

  const flushCommandQueue = useCallback(() => {
    const queue = commandQueueRef.current
    if (queue.length === 0) return
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    while (queue.length > 0) {
      const item = queue.shift()!
      try {
        socket.send(JSON.stringify({ command: item.command, payload: item.payload }))
        BootLogger.log('WebSocket', 'QUEUE_FLUSH', { command: item.command })
      } catch {
        queue.unshift(item)
        break
      }
    }
  }, [])

  const sendCommand = useCallback(
    (command: string, payload: unknown = {}): SendCommandResult => {
      if (!socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) {
        // Queue the command for later flush instead of dropping it
        if (command !== 'heartbeat_pong') {
          const queue = commandQueueRef.current
          if (queue.length < WS_COMMAND_QUEUE_MAX) {
            queue.push({ command, payload })
            BootLogger.log('WebSocket', 'COMMAND_QUEUED', {
              command,
              queueSize: queue.length,
            })
            setState((prev) => ({ ...prev, queuedCommands: queue.length }))
            return { ok: false, queued: true, message: '指令已排隊，連線恢復後自動送出。' }
          }
        }
        const errorMsg = '後端連線尚未就緒，指令未送出，請稍後再試。'
        setLastError(errorMsg)
        BootLogger.log('WebSocket', 'SEND_REJECTED_OFFLINE', { command }, 'warn')
        return { ok: false, queued: false, message: errorMsg }
      }

      try {
        socketRef.current.send(JSON.stringify({ command, payload }))
        BootLogger.log('WebSocket', 'SEND', { command })
        return { ok: true, queued: false }
      } catch {
        const errorMsg = 'WebSocket closed before the command was sent; command was queued'
        // Queue for retry
        if (command !== 'heartbeat_pong') {
          commandQueueRef.current.push({ command, payload })
        }
        return { ok: false, queued: true, message: errorMsg }
      }
    },
    []
  )

  useEffect(() => {
    let disposed = false
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
      // Exponential backoff with jitter: 500ms, 1s, 2s, 4s, 8s (max)
      // + up to 20% jitter to avoid thundering herd
      const baseDelay = Math.min(
        WS_RECONNECT_MAX_DELAY_MS,
        WS_RECONNECT_BASE_DELAY_MS * Math.pow(2, nextAttempt - 1)
      )
      const jitter = Math.random() * baseDelay * 0.2
      const delay = Math.round(baseDelay + jitter)
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void (async () => {
          await connect()
        })()
      }, delay)
      BootLogger.log('WebSocket', 'RECONNECT_SCHEDULED', {
        attempt: nextAttempt,
        delay,
      })
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
      let wsUrl = ''
      try {
        wsUrl = await getAuthenticatedBackendWebSocketUrl()
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        setLastError(message)
        updateBackendConnectionSnapshot('Error')
        setState((prev) => ({ ...prev, status: 'Error', lastError: message }))
        BootLogger.log('WebSocket', 'SESSION_FAILED', { error: message }, 'error')
        scheduleReconnect()
        return
      }
      if (disposed) return
      BootLogger.log('WebSocket', 'CONNECTING', { endpoint: '127.0.0.1:8765' })
      const socket = new WebSocket(wsUrl)
      socketRef.current = socket

      socket.onopen = () => {
        clearReconnectTimer()
        setLastError(null)
        updateBackendConnectionSnapshot('Synchronizing')
        setState((prev) => ({
          ...prev,
          status: 'Synchronizing',
          lastStatusAt: Date.now(),
          reconnectAttempt: reconnectAttemptRef.current,
          queuedCommands: commandQueueRef.current.length,
        }))
        BootLogger.log('WebSocket', 'OPEN', { endpoint: '127.0.0.1:8765' })
        socket.send(JSON.stringify({ command: 'app:get-runtime-status', payload: {} }))
      }

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(String(event.data)) as Record<string, unknown>

          if (payload.event === 'app:get-runtime-status_result') {
            const runtime = (payload.payload ?? {}) as Record<string, unknown>
            const ready =
              runtime.ok === true &&
              runtime.runtime_state === 'ready' &&
              runtime.governance_ready === true &&
              runtime.startup_dead !== true
            if (ready) {
              reconnectAttemptRef.current = 0
              updateBackendConnectionSnapshot('Connected', socket, true)
              setState((prev) => ({
                ...prev,
                status: 'Connected',
                reconnectAttempt: 0,
              }))
              eventBus.emit('socket_connected', { connected: true })
              flushCommandQueue()
              setState((prev) => ({ ...prev, queuedCommands: 0 }))
            } else {
              updateBackendConnectionSnapshot('Degraded', socket, false)
              setState((prev) => ({ ...prev, status: 'Degraded' }))
              eventBus.emit('socket_connected', { connected: false })
            }
          }

          // Respond to heartbeat ping immediately
          if (payload.event === 'heartbeat_ping') {
            try {
              socket.send(JSON.stringify({ command: 'heartbeat_pong', payload: {} }))
            } catch {
              // socket may have closed
            }
          }

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

          setState((prev) => ({
            ...prev,
            lastStatusAt: Date.now(),
          }))

          eventBus.emit('backend_message', payload)
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          BootLogger.log('WebSocket', 'PARSE_ERROR', { error: message }, 'error')
        }
      }

      socket.onerror = () => {
        const errorMsg = '後端連線中斷，系統正在自動修復。'
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
          lastError: '後端連線中斷，系統正在自動重新連線。',
        }))
        BootLogger.log('WebSocket', 'CLOSED')
        eventBus.emit('socket_connected', { connected: snapshot.connected })
        if (!disposed) {
          scheduleReconnect()
        }
      }
    }

    const reconnectNow = () => {
      if (document.visibilityState === 'hidden') return
      clearReconnectTimer()
      void connect()
    }
    window.addEventListener('online', reconnectNow)
    document.addEventListener('visibilitychange', reconnectNow)
    void connect()

    return () => {
      disposed = true
      window.removeEventListener('online', reconnectNow)
      document.removeEventListener('visibilitychange', reconnectNow)
      clearReconnectTimer()
      reconnectAttemptRef.current = 0
      commandQueueRef.current = []
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
      lastError: lastError ?? state.lastError,
    }),
    [state, sendCommand, lastError]
  )
}
