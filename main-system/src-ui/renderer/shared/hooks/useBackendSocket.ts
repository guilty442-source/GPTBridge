import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BootLogger } from '../BootLogger'
import { eventBus } from '../RuntimeEventBus'
import { getAuthenticatedBackendWebSocketUrl } from '../services/backendSession'
import {
  requestBackendRestart,
  resetBackendRecovery,
} from '../services/backendRecovery'
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
const WS_RECONNECT_MAX_DELAY_MS = 16000
const WS_CONNECT_TIMEOUT_MS = 8000
const WS_READINESS_RETRY_MS = 1500
const WS_COMMAND_QUEUE_MAX = 50
const WS_COMMAND_QUEUE_TTL_MS = 60_000
const WS_STALE_CONNECTION_MS = 15_000
const OUTBOX_CURSOR_KEY = 'gptbridge.outbox.cursor'
const OUTBOX_GENERATION_KEY = 'gptbridge.outbox.generation'
const OUTBOX_BUFFER_MAX = 500
const openBackendSockets = new Set<WebSocket>()

type OutboxStateEvent = {
  sequence: number
  entity_id: string
  entity_type: string
  operation: string
  authoritative_revision: number
  previous_revision: number
  changed_field_allowlist: string[]
  invalidation_keys: string[]
  state_hash: string
  backend_generation: string
  release_id: string
  contract_version: string
  correlation_id: string
  committed_at: string
  idempotency_key?: string
  session_id?: string
}

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
  const connectTimeoutRef = useRef<number | null>(null)
  const readinessTimerRef = useRef<number | null>(null)
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

    const clearReadinessTimer = () => {
      if (readinessTimerRef.current !== null) {
        window.clearTimeout(readinessTimerRef.current)
        readinessTimerRef.current = null
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
      BootLogger.log('WebSocket', 'CONNECTING', { endpoint: '127.0.0.1:8765' })
      const socket = new WebSocket(wsUrl)
      socketRef.current = socket
      clearConnectTimeout()
      connectTimeoutRef.current = window.setTimeout(() => {
        if (socket.readyState === WebSocket.CONNECTING) socket.close()
      }, WS_CONNECT_TIMEOUT_MS)

      const requestRuntimeStatus = () => {
        if (disposed || socket.readyState !== WebSocket.OPEN) return
        socket.send(JSON.stringify({ command: 'app:get-runtime-status', payload: {} }))
      }

      const scheduleReadinessCheck = () => {
        clearReadinessTimer()
        readinessTimerRef.current = window.setTimeout(() => {
          readinessTimerRef.current = null
          requestRuntimeStatus()
        }, WS_READINESS_RETRY_MS)
      }

      const applyRuntimeReadiness = (runtime: Record<string, unknown>) => {
        const ready =
          runtime.ok === true &&
          runtime.runtime_state === 'ready' &&
          runtime.governance_ready === true &&
          runtime.startup_dead !== true
        if (ready) {
          clearReadinessTimer()
          reconnectAttemptRef.current = 0
          resetBackendRecovery()
          updateBackendConnectionSnapshot('Connected', socket, true)
          setState((prev) => ({ ...prev, status: 'Connected', reconnectAttempt: 0 }))
          eventBus.emit('socket_connected', { connected: true })
          flushCommandQueue()
          setState((prev) => ({ ...prev, queuedCommands: 0 }))
        } else {
          updateBackendConnectionSnapshot('Synchronizing', socket, false)
          setState((prev) => ({ ...prev, status: 'Synchronizing' }))
          eventBus.emit('socket_connected', { connected: false })
          scheduleReadinessCheck()
          // A verified dead backend cannot recover by reconnecting — request a
          // governed boot_core restart (Electron main executes it). Cooldown +
          // attempt cap live in backendRecovery (FORBID:duplicate owner).
          if (runtime.startup_dead === true) {
            void requestBackendRestart('startup-dead').then((result) => {
              if (result.requested) {
                BootLogger.log('WebSocket', 'BACKEND_RESTART_REQUESTED', {
                  reason: result.reason,
                })
              }
            })
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
        BootLogger.log('WebSocket', 'OPEN', { endpoint: '127.0.0.1:8765' })
        requestRuntimeStatus()
        // A195 RECONNECT: resubscribe to the transactional outbox with the
        // last acknowledged cursor so the backend replays missed events.
        try {
          socket.send(
            JSON.stringify({
              command: 'state_event_hello',
              payload: { cursor: outboxAppliedRef.current },
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
          }

          // A195: transactional outbox state event — validate sequence,
          // apply once, acknowledge the contiguous cursor.
          if (payload.event === 'state_event') {
            const ev = payload.payload as OutboxStateEvent | undefined
            if (ev && typeof ev.sequence === 'number') {
              const storedGeneration = window.sessionStorage.getItem(
                OUTBOX_GENERATION_KEY
              )
              if (
                storedGeneration !== null &&
                ev.backend_generation !== storedGeneration
              ) {
                // Backend-generation change → invalidate the projection and
                // restart the stream from the authoritative cursor (A195 GAP).
                outboxAppliedRef.current = 0
                outboxBufferRef.current.clear()
                eventBus.emit('state_event_invalidate', {
                  reason: 'backend-generation-change',
                })
              }
              window.sessionStorage.setItem(
                OUTBOX_GENERATION_KEY,
                ev.backend_generation
              )

              if (ev.sequence <= outboxAppliedRef.current) {
                // Duplicate delivery (at-least-once) — dedupe via sequence.
              } else if (ev.sequence === outboxAppliedRef.current + 1) {
                outboxBufferRef.current.set(ev.sequence, ev)
                while (outboxBufferRef.current.has(outboxAppliedRef.current + 1)) {
                  const next = outboxBufferRef.current.get(
                    outboxAppliedRef.current + 1
                  )!
                  outboxBufferRef.current.delete(next.sequence)
                  outboxAppliedRef.current = next.sequence
                  window.sessionStorage.setItem(
                    OUTBOX_CURSOR_KEY,
                    String(next.sequence)
                  )
                  eventBus.emit('state_event', next)
                  eventBus.emit(`state_event:${next.entity_type}`, next)
                  if (next.entity_type === 'runtime-status') {
                    // Scoped snapshot convergence for the readiness entity.
                    requestRuntimeStatus()
                  }
                }
                try {
                  socket.send(
                    JSON.stringify({
                      command: 'state_event_ack',
                      payload: { cursor: outboxAppliedRef.current },
                    })
                  )
                } catch {
                  // ack is retried by the next delivered event
                }
              } else {
                // Sequence gap → buffer bounded, invalidate, request resync.
                if (outboxBufferRef.current.size < OUTBOX_BUFFER_MAX) {
                  outboxBufferRef.current.set(ev.sequence, ev)
                }
                eventBus.emit('state_event_invalidate', {
                  reason: 'sequence-gap',
                  expected: outboxAppliedRef.current + 1,
                  received: ev.sequence,
                })
                try {
                  socket.send(
                    JSON.stringify({
                      command: 'state_event_resync',
                      payload: { cursor: outboxAppliedRef.current },
                    })
                  )
                } catch {
                  // resync is retried on next event
                }
              }
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
        if (socket.readyState < WebSocket.CLOSING) socket.close()
      }

      socket.onclose = () => {
        clearConnectTimeout()
        clearReadinessTimer()
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
      clearConnectTimeout()
      clearReadinessTimer()
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
    }, 5_000)
    void connect()

    return () => {
      disposed = true
      window.removeEventListener('online', reconnectNow)
      document.removeEventListener('visibilitychange', reconnectNow)
      clearReconnectTimer()
      clearConnectTimeout()
      clearReadinessTimer()
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
