import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BootLogger } from '../BootLogger'
import { eventBus } from '../RuntimeEventBus'
import { getAuthenticatedBackendWebSocketUrl } from '../services/backendSession'
import { resetBackendRecovery } from '../services/backendRecovery'
import { mainSystemLocale } from '@/locales/main-system'
import {
  type BackendSocketState,
  type SendCommandResult,
  type OutboxStateEvent,
  INITIAL_STATE,
  WS_RECONNECT_BASE_DELAY_MS,
  WS_RECONNECT_MAX_DELAY_MS,
  WS_CONNECT_TIMEOUT_MS,
  WS_COMMAND_QUEUE_MAX,
  WS_COMMAND_QUEUE_TTL_MS,
  WS_STALE_CONNECTION_MS,
  OUTBOX_CURSOR_KEY,
  OUTBOX_GENERATION_KEY,
  OUTBOX_BUFFER_MAX,
  updateBackendConnectionSnapshot,
} from './useBackendSocketTypes'
import { handleOutboxSession, handleOutboxStateEvent } from './useBackendSocketOutbox'
import { applyRuntimeStatusReport } from '../services/runtimeStatusStore'

export { getBackendConnectionSnapshot } from './useBackendSocketTypes'

export const useBackendSocket = () => {
  const [state, setState] = useState<BackendSocketState>(INITIAL_STATE)
  const [lastError, setLastError] = useState<string | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const connectTimeoutRef = useRef<number | null>(null)
  const reconnectAttemptRef = useRef(0)
  const commandQueueRef = useRef<
    Array<{ command: string; payload: unknown; queuedAt: number }>
  >([])
  const lastMessageAtRef = useRef(0)
  // A195 transactional outbox client state: applied cursor survives socket
  // reconnects within the same window session (RECONNECT sends last-acked
  // cursor; gap → scoped invalidation + replay).
  const outboxAppliedRef = useRef<number>(
    Number(window.sessionStorage.getItem(OUTBOX_CURSOR_KEY) || 0) || 0
  )
  const outboxBufferRef = useRef<Map<number, OutboxStateEvent>>(new Map())
  // Generation reported by the current backend session.  Events stamped
  // with any other (superseded) generation are stale backlog replay and
  // must never be applied — doing so used to trigger a status-request
  // storm and exhaust the command rate limit.
  const sessionGenerationRef = useRef<string | null>(null)

  const ws = mainSystemLocale.websocket

  const flushCommandQueue = useCallback(() => {
    const queue = commandQueueRef.current
    if (queue.length === 0) return
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    while (queue.length > 0) {
      const item = queue.shift()!
      if (Date.now() - item.queuedAt > WS_COMMAND_QUEUE_TTL_MS) {
        BootLogger.log(
          'WebSocket',
          'QUEUE_ITEM_EXPIRED',
          { command: item.command },
          'warn'
        )
        continue
      }
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
            queue.push({ command, payload, queuedAt: Date.now() })
            BootLogger.log('WebSocket', 'COMMAND_QUEUED', {
              command,
              queueSize: queue.length,
            })
            setState((prev) => ({ ...prev, queuedCommands: queue.length }))
            return { ok: false, queued: true, message: ws.autoFlush }
          }
        }
        const errorMsg = ws.notReady
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
          if (commandQueueRef.current.length < WS_COMMAND_QUEUE_MAX) {
            commandQueueRef.current.push({
              command,
              payload,
              queuedAt: Date.now(),
            })
          }
        }
        return { ok: false, queued: true, message: errorMsg }
      }
    },
    []
  )

  useEffect(() => {
    let disposed = false
    let staleConnectionTimer: number | null = null
    const clearReconnectTimer = () => {
      const timer = reconnectTimerRef.current
      if (timer !== null) {
        window.clearTimeout(timer)
        reconnectTimerRef.current = null
      }
    }

    const clearConnectTimeout = () => {
      if (connectTimeoutRef.current !== null) {
        window.clearTimeout(connectTimeoutRef.current)
        connectTimeoutRef.current = null
      }
    }

    const clearStaleConnectionTimer = () => {
      if (staleConnectionTimer !== null) {
        window.clearInterval(staleConnectionTimer)
        staleConnectionTimer = null
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
      // Do NOT request a backend restart on reconnect exhaustion.
      // The backend process is likely still running — only the WebSocket
      // connection dropped. A backend restart is disruptive and should
      // only be triggered by a verified startup_dead condition (checked
      // in applyRuntimeReadiness), not by transient WebSocket failures.
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
      const endpointLabel = (() => {
        try {
          return new URL(wsUrl).host
        } catch {
          return 'backend'
        }
      })()
      BootLogger.log('WebSocket', 'CONNECTING', { endpoint: endpointLabel })
      const socket = new WebSocket(wsUrl)
      socketRef.current = socket
      clearConnectTimeout()
      connectTimeoutRef.current = window.setTimeout(() => {
        if (socket.readyState === WebSocket.CONNECTING) socket.close()
      }, WS_CONNECT_TIMEOUT_MS)

      const applyRuntimeReadiness = (runtime: Record<string, unknown>) => {
        const ready =
          runtime.ok === true &&
          runtime.runtime_state === 'ready' &&
          runtime.governance_ready === true &&
          runtime.startup_dead !== true
        if (ready) {
          reconnectAttemptRef.current = 0
          resetBackendRecovery()
          updateBackendConnectionSnapshot('Connected', socket, true)
          setState((prev) => ({ ...prev, status: 'Connected', reconnectAttempt: 0 }))
          eventBus.emit('socket_connected', { connected: true })
          flushCommandQueue()
          setState((prev) => ({ ...prev, queuedCommands: 0 }))
        } else {
          // No polling: the backend pushes a fresh report every cycle and on
          // every readiness change, so this state converges without requests.
          updateBackendConnectionSnapshot('Synchronizing', socket, false)
          setState((prev) => ({ ...prev, status: 'Synchronizing' }))
          eventBus.emit('socket_connected', { connected: false })
          // A195/A196: a startup_dead payload means the
          // backend PROCESS is alive (it just sent us a message) but its
          // runtime init failed — the governed recovery owner is boot_core,
          // not the UI socket.  Surface a typed degraded signal instead.
          if (runtime.startup_dead === true) {
            eventBus.emit('backend_startup_dead', {
              runtime_state: runtime.runtime_state,
            })
            BootLogger.log(
              'WebSocket',
              'BACKEND_STARTUP_DEAD_DEGRADED',
              {},
              'warn'
            )
          }
        }
      }

      socket.onopen = () => {
        clearConnectTimeout()
        clearReconnectTimer()
        setLastError(null)
        lastMessageAtRef.current = Date.now()
        updateBackendConnectionSnapshot('Synchronizing')
        setState((prev) => ({
          ...prev,
          status: 'Synchronizing',
          lastStatusAt: Date.now(),
          reconnectAttempt: reconnectAttemptRef.current,
          queuedCommands: commandQueueRef.current.length,
        }))
        BootLogger.log('WebSocket', 'OPEN', { endpoint: endpointLabel })
        // The backend owns the refresh: it sends an immediate health report
        // on connection plus a report every cycle.  The client never polls.
        // A195 RECONNECT: resubscribe to the transactional outbox with the
        // last acknowledged cursor so the backend replays missed events.
        try {
          socket.send(
            JSON.stringify({
              command: 'state_event_hello',
              payload: {
                cursor: outboxAppliedRef.current,
                generation:
                  window.sessionStorage.getItem(OUTBOX_GENERATION_KEY) || '',
              },
            })
          )
        } catch {
          // socket may have closed; hello is retried on next open
        }
      }

      socket.onmessage = (event) => {
        lastMessageAtRef.current = Date.now()
        try {
          const payload = JSON.parse(String(event.data)) as Record<string, unknown>

          if (
            payload.event === 'app:get-runtime-status_result' ||
            payload.event === 'runtime_status_push'
          ) {
            const runtime = (payload.payload ?? {}) as Record<string, unknown>
            applyRuntimeReadiness(runtime)
            // Modular distribution: each module subscribes to its own field.
            applyRuntimeStatusReport(
              runtime as Parameters<typeof applyRuntimeStatusReport>[0]
            )
          }

          // A195: outbox session answer — a generation change means the
          // backend restarted; invalidate the projection and resume the
          // stream from sequence 0 instead of trusting the stale cursor.
          if (payload.event === 'state_event_session') {
            handleOutboxSession(payload.payload, {
              outboxAppliedRef,
              outboxBufferRef,
              sessionGenerationRef,
            })
          }

          // A195: transactional outbox state event — validate sequence,
          // apply once, acknowledge the contiguous cursor.
          if (payload.event === 'state_event') {
            handleOutboxStateEvent(
              payload.payload,
              {
                outboxAppliedRef,
                outboxBufferRef,
                sessionGenerationRef,
              },
              (command, p) => {
                try {
                  socket.send(JSON.stringify({ command, payload: p }))
                } catch {
                  // retried by the next delivered event
                }
              },
              () => undefined
            )
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
        const errorMsg = ws.autoRepairing
        setLastError(errorMsg)
        updateBackendConnectionSnapshot('Error')
        setState((prev) => ({
          ...prev,
          status: 'Error',
          lastError: errorMsg,
        }))
        BootLogger.log('WebSocket', 'ERROR', {}, 'error')
        if (socket.readyState < WebSocket.CLOSING) socket.close()
      }

      socket.onclose = () => {
        clearConnectTimeout()
        if (socketRef.current === socket) {
          socketRef.current = null
        }
        const snapshot = updateBackendConnectionSnapshot('Disconnected', socket, false)
        setState((prev) => ({
          ...prev,
          status: 'Disconnected',
          lastError: ws.autoReconnecting,
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
      clearConnectTimeout()
      void connect()
    }
    window.addEventListener('online', reconnectNow)
    document.addEventListener('visibilitychange', reconnectNow)
    staleConnectionTimer = window.setInterval(() => {
      const socket = socketRef.current
      if (
        socket?.readyState === WebSocket.OPEN &&
        lastMessageAtRef.current > 0 &&
        Date.now() - lastMessageAtRef.current > WS_STALE_CONNECTION_MS
      ) {
        BootLogger.log('WebSocket', 'STALE_CONNECTION_CLOSED', {}, 'warn')
        socket.close(4000, 'stale-connection')
      }
    }, 3_000)
    void connect()

    return () => {
      disposed = true
      window.removeEventListener('online', reconnectNow)
      document.removeEventListener('visibilitychange', reconnectNow)
      clearReconnectTimer()
      clearConnectTimeout()
      clearStaleConnectionTimer()
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
