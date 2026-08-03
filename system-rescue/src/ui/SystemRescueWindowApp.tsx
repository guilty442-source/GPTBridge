import { useEffect, useRef, useState } from 'react'

type RunResult = {
  ok?: boolean
  request_id?: string
  stdout?: string
  stderr?: string
  message?: string
}

export function SystemRescueWindowApp() {
  const socket = useRef<WebSocket | null>(null)
  const waiters = useRef(new Map<string, (result: RunResult) => void>())
  const [status, setStatus] = useState('連線中')
  const [busy, setBusy] = useState(false)
  const [output, setOutput] = useState('系統救援已就緒')

  useEffect(() => {
    let disposed = false
    const connect = async () => {
      const api = (window as any).electron
      const started = await api.invoke('app:ensure-backend-started')
      if (!started?.ok || disposed) throw new Error(started?.message || '後端啟動失敗')
      const session = await api.invoke('app:get-backend-session')
      const url = new URL(String(session?.websocketUrl || ''))
      if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1') {
        throw new Error('權限不足')
      }
      const connection = new WebSocket(url)
      socket.current = connection
      connection.onopen = () => setStatus('已連線')
      connection.onclose = () => setStatus('已中斷')
      connection.onerror = () => setStatus('連線錯誤')
      connection.onmessage = (event) => {
        const frame = JSON.parse(String(event.data))
        if (frame.event !== 'toolbox_run_tool_result') return
        const result = frame.payload as RunResult
        const resolve = waiters.current.get(String(result.request_id || ''))
        if (resolve) {
          waiters.current.delete(String(result.request_id || ''))
          resolve(result)
        }
      }
    }
    void connect().catch((error) => setStatus(error instanceof Error ? error.message : '連線錯誤'))
    return () => {
      disposed = true
      socket.current?.close()
      socket.current = null
    }
  }, [])

  const run = async (args: string[]) => {
    if (!socket.current || socket.current.readyState !== WebSocket.OPEN || busy) return
    setBusy(true)
    const requestId = `system-rescue:${Date.now()}:${Math.random().toString(16).slice(2)}`
    const result = await new Promise<RunResult>((resolve) => {
      waiters.current.set(requestId, resolve)
      socket.current!.send(JSON.stringify({
        command: 'toolbox_run_tool',
        payload: { tool_id: 'system-rescue', request_id: requestId, args },
      }))
    })
    setOutput([result.message, result.stdout, result.stderr].filter(Boolean).join('\n'))
    setBusy(false)
  }

  return (
    <main style={{ minHeight: '100vh', padding: 28, boxSizing: 'border-box', background: '#0b0f17', color: '#f8fafc', fontFamily: '"Noto Sans TC", "Segoe UI", sans-serif' }}>
      <section style={{ maxWidth: 860, margin: '0 auto', padding: 24, border: '1px solid #263449', borderRadius: 12, background: '#111827' }}>
        <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div><h1 style={{ margin: 0 }}>系統救援</h1><p style={{ color: '#94a3b8' }}>集中執行故障診斷、資料庫修復與工具重建；主系統只負責送件與重新啟動</p></div>
          <span>{status}</span>
        </header>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', margin: '20px 0' }}>
          <button disabled={busy || status !== '已連線'} onClick={() => void run(['--check', '--json'])}>檢查系統</button>
          <button disabled={busy || status !== '已連線'} onClick={() => void run(['--repair-owned-storage', '--json'])}>清理救援日誌</button>
        </div>
        <pre style={{ minHeight: 320, padding: 16, overflow: 'auto', whiteSpace: 'pre-wrap', borderRadius: 8, background: '#08101d' }}>{busy ? '執行中…' : output}</pre>
      </section>
    </main>
  )
}
