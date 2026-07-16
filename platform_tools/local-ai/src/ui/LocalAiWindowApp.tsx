import { useCallback, useEffect, useRef, useState } from 'react'
import './local-ai.css'

type BackendFrame = {
  event?: string
  payload?: Record<string, unknown>
}

export function LocalAiWindowApp() {
  const [connection, setConnection] = useState('連線中')
  const [modelStatus, setModelStatus] = useState('尚未檢查')
  const [prompt, setPrompt] = useState('')
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<number | null>(null)

  const send = useCallback((command: string, payload: Record<string, unknown> = {}) => {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setAnswer('後端連線尚未就緒，指令未送出，請稍後再試。')
      return false
    }
    socket.send(JSON.stringify({ command, payload }))
    return true
  }, [])

  useEffect(() => {
    let disposed = false
    const connect = async () => {
      if (disposed) return
      try {
        const api = (window as any).electron
        if (!api?.invoke) throw new Error('IPC bridge unavailable')
        const started = await api.invoke('app:ensure-backend-started')
        if (!started?.ok) throw new Error(String(started?.message || 'backend unavailable'))
        const session = await api.invoke('app:get-backend-session')
        const url = new URL(String(session?.websocketUrl || ''))
        const port = Number(url.port)
        if (
          url.protocol !== 'ws:' ||
          url.hostname !== '127.0.0.1' ||
          !Number.isInteger(port) ||
          port < 1024 ||
          port > 65535 ||
          !url.searchParams.get('token') ||
          !url.searchParams.get('instance')
        ) throw new Error('invalid authenticated session')
        const socket = new WebSocket(url.toString())
        socketRef.current = socket
        socket.onopen = () => {
          setConnection('已連線')
          send('local_ai_status')
        }
        socket.onmessage = (event) => {
          try {
            const frame = JSON.parse(String(event.data)) as BackendFrame
            const payload = frame.payload || {}
            if (frame.event === 'local_ai_status_result') {
              setModelStatus(
                payload.ok === true
                  ? String(payload.message || payload.status || '可用')
                  : String(payload.message || '模型尚未就緒')
              )
            }
            if (frame.event === 'local_ai_infer_result') {
              setBusy(false)
              setAnswer(String(payload.response || payload.message || '沒有回覆'))
            }
          } catch {
            // Ignore malformed backend frames.
          }
        }
        socket.onerror = () => setConnection('連線錯誤')
        socket.onclose = () => {
          if (socketRef.current === socket) socketRef.current = null
          setConnection('未連線')
          if (!disposed) reconnectRef.current = window.setTimeout(connect, 1500)
        }
      } catch {
        setConnection('未連線')
        if (!disposed) reconnectRef.current = window.setTimeout(connect, 1500)
      }
    }
    void connect()
    return () => {
      disposed = true
      if (reconnectRef.current !== null) window.clearTimeout(reconnectRef.current)
      const socket = socketRef.current
      socketRef.current = null
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close()
    }
  }, [send])

  const infer = () => {
    const value = prompt.trim()
    if (!value) return
    setBusy(true)
    if (!send('local_ai_infer', { prompt: value })) setBusy(false)
  }

  return (
    <main className="local-ai-shell">
      <header>
        <div>
          <p className="eyebrow">GPTBridge 本地 AI</p>
          <h1>星澄</h1>
        </div>
        <span className={connection === '已連線' ? 'status ready' : 'status'}>{connection}</span>
      </header>
      <section className="model-card">
        <h2>本機模型狀態</h2>
        <p>{modelStatus}</p>
        <button type="button" onClick={() => send('local_ai_status')}>重新檢查</button>
      </section>
      <section className="prompt-card">
        <label htmlFor="local-ai-prompt">訊息</label>
        <textarea
          id="local-ai-prompt"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="輸入要交給本地模型處理的內容"
        />
        <button type="button" disabled={busy} onClick={infer}>{busy ? '處理中…' : '送出'}</button>
        <pre>{answer || '回覆會顯示在這裡。'}</pre>
      </section>
      <footer>資料與模型連線僅限本工具；斷線指令不排隊。</footer>
    </main>
  )
}
