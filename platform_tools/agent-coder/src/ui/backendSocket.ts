import { useCallback, useEffect, useRef, useState } from 'react'

export type SendCommandResult = {
  ok: boolean
  queued: boolean
  message?: string
}

export function useLocalBackendSocket() {
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
        // The socket may already be connected by another window.
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
          // Ignore malformed backend frames in application windows.
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
