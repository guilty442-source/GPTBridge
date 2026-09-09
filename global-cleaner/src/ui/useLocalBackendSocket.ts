import { useCallback, useEffect, useRef, useState } from 'react'

const RECONNECT_MAX_DELAY_MS = 8_000

export type SendCommandResult = {
  ok: boolean
  queued: boolean
  queueId?: string
  message?: string
}

export type SendCommandOptions = {
  allowOfflineQueue?: boolean
  queueTtlMs?: number
  onQueueExpired?: (error: Error) => void
}

export type BackendSession = {
  token?: string
  websocketUrl?: string
}


export function useLocalBackendSocket(requestIds?: Set<string>) {
  const [status, setStatus] = useState('Disconnected')
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const reconnectAttemptRef = useRef(0)

  const removeQueuedCommand = useCallback((_queueId: string): boolean => false, [])

  const sendCommand = useCallback(
    (
      command: string,
      payload: unknown = {},
      _options: SendCommandOptions = {}
    ): SendCommandResult => {
      const socket = socketRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        return {
          ok: false,
          queued: false,
          message: '後端連線尚未就緒，指令未送出，請稍後再試。',
        }
      }

      try {
        socket.send(JSON.stringify({ command, payload }))
        return { ok: true, queued: false }
      } catch {
        return {
          ok: false,
          queued: false,
          message: 'Backend connection closed before the command was sent.',
        }
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
      if (!api?.invoke) return
      try {
        await api.invoke('app:ensure-backend-started')
      } catch {
        // Tool windows can still connect if the backend is already running.
      }
    }

    const backendWebSocketUrl = async () => {
      const api = (window as any).electron
      if (!api?.invoke) throw new Error('Electron IPC bridge is unavailable')
      const session = (await api.invoke('app:get-backend-session')) as
        | BackendSession
        | null
        | undefined
      const token = String(session?.token || '').trim()
      const websocketUrl = String(session?.websocketUrl || '').trim()
      if (!token || !websocketUrl) {
        throw new Error('Backend session capability is unavailable')
      }
      const url = new URL(websocketUrl)
      if (
        (url.protocol !== 'ws:' && url.protocol !== 'wss:') ||
        url.hostname !== '127.0.0.1'
      ) {
        throw new Error('Backend WebSocket endpoint is invalid')
      }
      url.searchParams.set('token', token)
      return url.toString()
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

      setStatus('Connecting')
      await ensureBackendStarted()
      if (disposed) return

      let websocketUrl = ''
      try {
        websocketUrl = await backendWebSocketUrl()
      } catch {
        setStatus('Error')
        scheduleReconnect()
        return
      }
      if (disposed) return
      let socket: WebSocket
      try {
        socket = new WebSocket(websocketUrl)
      } catch {
        setStatus('Error')
        scheduleReconnect()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
          setStatus('Connected')
          reconnectAttemptRef.current = 0
        clearReconnectTimer()
        window.dispatchEvent(
          new CustomEvent('socket_connected', { detail: { connected: true } })
        )
      }

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(String(event.data)) as Record<string, unknown>
          if (payload.event && typeof payload.event === 'string') {
            const eventPayload =
              payload.payload && typeof payload.payload === 'object'
                ? (payload.payload as Record<string, unknown>)
                : {}
            if (
              payload.event === 'toolbox_request_tool_execution_progress' &&
              requestIds &&
              String(eventPayload.tool_id || '') === 'global-cleaner' &&
              !requestIds.has(
                String(eventPayload.request_id || '')
              )
            ) {
              return
            }
            window.dispatchEvent(
              new CustomEvent('ipc_event', {
                detail: {
                  event: payload.event,
                  payload: eventPayload,
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
        if (socket.readyState < WebSocket.CLOSING) socket.close()
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

    const reconnectNow = () => {
      if (document.visibilityState === 'hidden') return
      clearReconnectTimer()
      reconnectAttemptRef.current = 0
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
      const socket = socketRef.current
      socketRef.current = null
      if (socket && socket.readyState < WebSocket.CLOSING) {
        socket.close()
      }
    }
  }, [])

  return { removeQueuedCommand, sendCommand, status }
}
