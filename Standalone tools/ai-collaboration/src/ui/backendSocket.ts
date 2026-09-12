import { useCallback, useEffect, useRef, useState } from 'react'

const RECONNECT_MAX_DELAY_MS = 8_000

export type SendCommandResult = {
  ok: boolean
  queued: false
  message?: string
}

type ConnectionWaiter = {
  timer: number
  resolve: () => void
  reject: (error: Error) => void
}

function mutationId(command: string): string {
  const suffix =
    typeof globalThis.crypto?.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `${command}:${suffix}`
}

function withIdempotencyKey(command: string, payload: unknown): unknown {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return payload
  const record = payload as Record<string, unknown>
  if (String(record.idempotency_key || '').trim()) return payload
  return {
    ...record,
    idempotency_key:
      String(record.operation_id || '').trim() ||
      String(record.request_id || '').trim() ||
      mutationId(command),
  }
}

function dispatchLocalFailure(command: string, payload: unknown, message: string) {
  const requestId =
    payload && typeof payload === 'object' && !Array.isArray(payload)
      ? String((payload as Record<string, unknown>).request_id || '')
      : ''
  window.dispatchEvent(
    new CustomEvent('ipc_event', {
      detail: {
        event: `${command}_result`,
        payload: { ok: false, queued: false, message, request_id: requestId },
      },
    })
  )
}

export function useLocalBackendSocket() {
  const [status, setStatus] = useState('Disconnected')
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const connectionWaitersRef = useRef<Set<ConnectionWaiter>>(new Set())
  const connectGenerationRef = useRef(0)
  const reconnectAttemptRef = useRef(0)

  const waitUntilConnected = useCallback((timeoutMs = 15_000) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) return Promise.resolve()
    return new Promise<void>((resolve, reject) => {
      const waiter: ConnectionWaiter = {
        timer: 0,
        resolve: () => {
          window.clearTimeout(waiter.timer)
          connectionWaitersRef.current.delete(waiter)
          resolve()
        },
        reject: (error: Error) => {
          window.clearTimeout(waiter.timer)
          connectionWaitersRef.current.delete(waiter)
          reject(error)
        },
      }
      waiter.timer = window.setTimeout(
        () => waiter.reject(new Error('後端連線等待逾時，指令未送出。')),
        Math.max(1_000, Math.min(60_000, timeoutMs))
      )
      connectionWaitersRef.current.add(waiter)
      if (socketRef.current?.readyState === WebSocket.OPEN) waiter.resolve()
    })
  }, [])

  const sendCommand = useCallback(
    (command: string, payload: unknown = {}): SendCommandResult => {
      const preparedPayload = withIdempotencyKey(command, payload)
      const socket = socketRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        const message = '後端連線尚未就緒，指令未送出，請稍後再試。'
        dispatchLocalFailure(command, preparedPayload, message)
        return { ok: false, queued: false, message }
      }
      try {
        socket.send(JSON.stringify({ command, payload: preparedPayload }))
        return { ok: true, queued: false }
      } catch {
        const message = 'WebSocket 傳送失敗，指令未送出。'
        dispatchLocalFailure(command, preparedPayload, message)
        return { ok: false, queued: false, message }
      }
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
      if (!api?.invoke) throw new Error('Electron IPC bridge is unavailable')
      const result = await api.invoke('app:ensure-backend-started')
      if (!result || result.ok !== true) {
        throw new Error(String(result?.message || 'Backend startup failed'))
      }
    }

    const backendWebSocketUrl = async () => {
      const api = (window as any).electron
      if (!api?.invoke) throw new Error('Electron IPC bridge is unavailable')
      const session = await api.invoke('app:get-backend-session')
      const websocketUrl = String(session?.websocketUrl || '')
      const parsed = new URL(websocketUrl)
      const port = Number(parsed.port)
      if (
        parsed.protocol !== 'ws:' ||
        parsed.hostname !== '127.0.0.1' ||
        parsed.username ||
        parsed.password ||
        !Number.isInteger(port) ||
        port < 1024 ||
        port > 65535
      ) {
        throw new Error('Backend session capability is unavailable')
      }
      return parsed.toString()
    }

    const scheduleReconnect = () => {
      if (disposed || reconnectTimerRef.current !== null) return
      const attempt = ++reconnectAttemptRef.current
      const baseDelay = Math.min(RECONNECT_MAX_DELAY_MS, 500 * 2 ** (attempt - 1))
      const delay = Math.round(baseDelay + Math.random() * baseDelay * 0.2)
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void connect()
      }, delay)
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
      const generation = ++connectGenerationRef.current
      setStatus('Connecting')
      try {
        await ensureBackendStarted()
        if (disposed || generation !== connectGenerationRef.current) return
        const websocketUrl = await backendWebSocketUrl()
        if (disposed || generation !== connectGenerationRef.current) return
        const socket = new WebSocket(websocketUrl)
        socketRef.current = socket
        socket.onopen = () => {
          if (disposed || generation !== connectGenerationRef.current) {
            socket.close()
            return
          }
          setStatus('Connected')
          reconnectAttemptRef.current = 0
          clearReconnectTimer()
          for (const waiter of [...connectionWaitersRef.current]) waiter.resolve()
          window.dispatchEvent(
            new CustomEvent('socket_connected', { detail: { connected: true } })
          )
        }
        socket.onmessage = (event) => {
          if (disposed || socketRef.current !== socket) return
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
            // Ignore malformed backend frames in application windows.
          }
        }
        socket.onerror = () => {
          if (!disposed && socketRef.current === socket) setStatus('Error')
          if (socket.readyState < WebSocket.CLOSING) socket.close()
        }
        socket.onclose = () => {
          if (socketRef.current === socket) socketRef.current = null
          if (disposed || generation !== connectGenerationRef.current) return
          setStatus('Disconnected')
          window.dispatchEvent(
            new CustomEvent('socket_connected', { detail: { connected: false } })
          )
          scheduleReconnect()
        }
      } catch (error) {
        if (disposed || generation !== connectGenerationRef.current) return
        setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`)
        scheduleReconnect()
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
      connectGenerationRef.current += 1
      reconnectAttemptRef.current = 0
      clearReconnectTimer()
      for (const waiter of [...connectionWaitersRef.current]) {
        waiter.reject(new Error('外部協作視窗已關閉，指令未送出。'))
      }
      const socket = socketRef.current
      socketRef.current = null
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close()
      }
    }
  }, [])

  return { sendCommand, status, waitUntilConnected }
}
