import { useCallback, useEffect, useRef, useState } from 'react'

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


export function useLocalBackendSocket(
  onMessageFilter?: (event: string, payload: Record<string, unknown>) => boolean
) {
  const [status, setStatus] = useState('Disconnected')
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)

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
          message: 'WebSocket 連線在送出前中斷；指令未加入佇列。',
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
      if (!api?.invoke) {
        throw new Error('目前環境無法啟動後端服務。')
      }
      const result = await api.invoke('app:ensure-backend-started')
      if (!result || result.ok !== true) {
        throw new Error(
          String(result?.message || '後端啟動失敗，且未提供原因。')
        )
      }
    }

    const getAuthenticatedSocketUrl = async (): Promise<string> => {
      const api = (window as any).electron
      if (!api?.invoke) {
        throw new Error('目前環境無法取得後端連線憑證。')
      }
      const session = (await api.invoke('app:get-backend-session')) as
        | BackendSession
        | null
        | undefined
      const token = String(session?.token || '').trim()
      const websocketUrl = String(session?.websocketUrl || '').trim()
      if (!token || !websocketUrl) {
        throw new Error('後端連線憑證尚未就緒。')
      }

      const url = new URL(websocketUrl)
      const backendPort = Number(url.port)
      if (
        url.protocol !== 'ws:' ||
        url.hostname !== '127.0.0.1' ||
        url.username ||
        url.password ||
        !Number.isInteger(backendPort) ||
        backendPort < 1024 ||
        backendPort > 65535
      ) {
        throw new Error('後端提供了不支援的 WebSocket 位址。')
      }
      url.searchParams.set('token', token)
      return url.toString()
    }

    const scheduleReconnect = () => {
      if (disposed || reconnectTimerRef.current !== null) return
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void connect()
      }, 500)
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
      try {
        await ensureBackendStarted()
      } catch {
        if (disposed) return
        setStatus('Error')
        scheduleReconnect()
        return
      }
      if (disposed) return

      let socketUrl = ''
      try {
        socketUrl = await getAuthenticatedSocketUrl()
      } catch {
        setStatus('Error')
        scheduleReconnect()
        return
      }
      if (disposed) return

      let socket: WebSocket
      try {
        socket = new WebSocket(socketUrl)
      } catch {
        setStatus('Error')
        scheduleReconnect()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        setStatus('Connected')
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
            if (onMessageFilter?.(payload.event, eventPayload)) return
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
  }, [onMessageFilter])

  return { removeQueuedCommand, sendCommand, status }
}
