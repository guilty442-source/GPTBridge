import { useCallback, useEffect, useRef, useState } from 'react'

export type ConnectionStatus = 'Connecting' | 'Connected' | 'Disconnected' | 'Error'

type BackendSession = { token?: string; websocketUrl?: string }
type PendingRequest = {
  command: string
  event: string
  progress?: (payload: Record<string, unknown>) => void
  resolve: (payload: Record<string, unknown>) => void
  reject: (error: Error) => void
  timer: number
}

function timeoutMessage(command: string): string {
  if (command === 'star_chat_status') {
    return '星澄狀態檢查逾時，請確認後端服務是否已啟動。'
  }
  return '星澄回應逾時，請確認模型服務是否已啟動。'
}

export function useStarChatBackend() {
  const [status, setStatus] = useState<ConnectionStatus>('Disconnected')
  const socketRef = useRef<WebSocket | null>(null)
  const pendingRef = useRef(new Map<string, PendingRequest>())
  const reconnectTimerRef = useRef<number | null>(null)

  const request = useCallback(
    (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 230_000,
      onProgress?: (payload: Record<string, unknown>) => void
    ): Promise<Record<string, unknown>> => {
      const socket = socketRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        return Promise.reject(new Error('後端仍在等待連線，請稍後再試。'))
      }
      const requestId = `star-chat:${Date.now()}:${crypto.randomUUID()}`
      return new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => {
          pendingRef.current.delete(requestId)
          reject(new Error(timeoutMessage(command)))
        }, timeoutMs)
        pendingRef.current.set(requestId, {
          command,
          event: `${command}_result`,
          progress: onProgress,
          resolve,
          reject,
          timer,
        })
        try {
          socket.send(JSON.stringify({ command, payload: { ...payload, request_id: requestId } }))
        } catch {
          window.clearTimeout(timer)
          pendingRef.current.delete(requestId)
          reject(new Error('送出訊息時連線中斷。'))
        }
      })
    },
    []
  )

  const cancelRequests = useCallback((command: string): number => {
    const socket = socketRef.current
    let cancelled = 0
    for (const [requestId, pending] of pendingRef.current.entries()) {
      if (pending.command !== command) continue
      window.clearTimeout(pending.timer)
      pendingRef.current.delete(requestId)
      pending.reject(new Error('已停止產生回答。'))
      cancelled += 1
      if (socket?.readyState === WebSocket.OPEN) {
        try {
          socket.send(JSON.stringify({
            command: 'toolbox_cancel_tool_run',
            payload: { request_id: requestId },
          }))
        } catch {
          // The local request is already stopped; reconnect logic owns the socket.
        }
      }
    }
    return cancelled
  }, [])

  useEffect(() => {
    let disposed = false

    const rejectPending = (message: string) => {
      for (const pending of pendingRef.current.values()) {
        window.clearTimeout(pending.timer)
        pending.reject(new Error(message))
      }
      pendingRef.current.clear()
    }

    const scheduleReconnect = () => {
      if (disposed || reconnectTimerRef.current !== null) return
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        void connect()
      }, 500)
    }

    const socketUrl = async (): Promise<string> => {
      const api = (window as any).electron
      if (!api?.invoke) throw new Error('Electron IPC bridge unavailable')
      const started = await api.invoke('app:ensure-backend-started')
      if (!started || started.ok !== true) throw new Error('Backend unavailable')
      const session = (await api.invoke('app:get-backend-session')) as BackendSession
      const token = String(session?.token || '').trim()
      const rawUrl = String(session?.websocketUrl || '').trim()
      const url = new URL(rawUrl)
      const port = Number(url.port)
      if (
        !token ||
        url.protocol !== 'ws:' ||
        url.hostname !== '127.0.0.1' ||
        url.username ||
        url.password ||
        !Number.isInteger(port) ||
        port < 1024 ||
        port > 65535
      ) {
        throw new Error('Invalid backend session')
      }
      url.searchParams.set('token', token)
      return url.toString()
    }

    const connect = async () => {
      if (disposed) return
      const active = socketRef.current
      if (active && active.readyState < WebSocket.CLOSING) return
      setStatus('Connecting')
      try {
        const url = await socketUrl()
        if (disposed) return
        const socket = new WebSocket(url)
        socketRef.current = socket
        socket.onopen = () => setStatus('Connected')
        socket.onmessage = (event) => {
          try {
            const frame = JSON.parse(String(event.data)) as {
              event?: unknown
              payload?: Record<string, unknown>
            }
            const result = frame.payload
            const requestId = String(result?.request_id || '')
            let pendingKey = requestId
            let pending = pendingRef.current.get(requestId)
            if (!pending && pendingRef.current.size === 1) {
              const only = pendingRef.current.entries().next().value as
                | [string, PendingRequest]
                | undefined
              if (
                only
                && (
                  frame.event === only[1].event
                  || frame.event === `${only[1].command}_progress`
                  || frame.event === 'error'
                )
              ) {
                pendingKey = only[0]
                pending = only[1]
              }
            }
            if (!pending) return
            if (frame.event === 'error') {
              window.clearTimeout(pending.timer)
              pendingRef.current.delete(pendingKey)
              pending.reject(new Error(String(result?.message || '後端處理失敗。')))
              return
            }
            if (frame.event === `${pending.command}_progress`) {
              pending.progress?.(result || {})
              return
            }
            if (frame.event !== pending.event) return
            window.clearTimeout(pending.timer)
            pendingRef.current.delete(pendingKey)
            pending.resolve(result || {})
          } catch {
            // Ignore frames which do not match the governed response contract.
          }
        }
        socket.onerror = () => {
          setStatus('Error')
          if (socket.readyState < WebSocket.CLOSING) socket.close()
        }
        socket.onclose = () => {
          if (socketRef.current === socket) socketRef.current = null
          setStatus('Disconnected')
          rejectPending('後端連線中斷，請重新送出訊息。')
          scheduleReconnect()
        }
      } catch {
        setStatus('Error')
        scheduleReconnect()
      }
    }

    const reconnectNow = () => {
      if (document.visibilityState === 'hidden') return
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      void connect()
    }
    window.addEventListener('online', reconnectNow)
    document.addEventListener('visibilitychange', reconnectNow)
    void connect()
    return () => {
      disposed = true
      window.removeEventListener('online', reconnectNow)
      document.removeEventListener('visibilitychange', reconnectNow)
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current)
      rejectPending('對話視窗已關閉。')
      const socket = socketRef.current
      socketRef.current = null
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close()
    }
  }, [])

  return { request, cancelRequests, status }
}
