import { useState, useEffect } from 'react'
import { mainSystemLocale } from '@/locales/main-system'
import './AppExternalCollaborationDrawer.css'

const ec = mainSystemLocale.externalCollaboration
const t = mainSystemLocale.toolbox

interface ExternalCollaborationDrawerProps {
  sendCommand: (command: string, payload?: unknown) => { ok: boolean; queued: boolean; message?: string }
  waitForIpcEvent: (
    eventName: string,
    timeoutMs: number,
    predicate?: (payload: Record<string, unknown>) => boolean
  ) => Promise<Record<string, unknown>>
  backendSocket: { status: string }
}

const COLLABORATION_ACTION_TIMEOUT_MS = 120000

export function ExternalCollaborationDrawer({
  sendCommand,
  waitForIpcEvent,
  backendSocket,
}: ExternalCollaborationDrawerProps) {
  const [status, setStatus] = useState<'stopped' | 'running' | 'error'>('stopped')
  const [busy, setBusy] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string>('')

  const connected = backendSocket.status === 'Connected'

  const handleStart = async () => {
    if (busy || !connected) return
    setBusy(true)
    setErrorMessage('')
    setStatus('running')
    const requestId = `ai-collaboration:start:${Date.now()}:${Math.random().toString(16).slice(2)}`
    try {
      const waitResult = waitForIpcEvent('toolbox_start_tool_result', COLLABORATION_ACTION_TIMEOUT_MS, (payload) =>
        String(payload.request_id || '') === requestId
      )
      const sent = sendCommand('toolbox_start_tool', { tool_id: 'ai-collaboration', request_id: requestId })
      if (!sent.ok) throw new Error(sent.message || t.startFailed)
      const result = await waitResult
      if (result.ok === false) throw new Error(String(result.message || t.startFailed))
      setStatus('running')
    } catch (error) {
      const message = error instanceof Error ? error.message : t.startFailed
      setStatus('error')
      setErrorMessage(message)
    } finally {
      setBusy(false)
    }
  }

  const handleStop = async () => {
    if (busy || !connected) return
    setBusy(true)
    setErrorMessage('')
    const requestId = `ai-collaboration:stop:${Date.now()}:${Math.random().toString(16).slice(2)}`
    try {
      const waitResult = waitForIpcEvent('toolbox_force_close_tool_result', COLLABORATION_ACTION_TIMEOUT_MS, (payload) =>
        String(payload.request_id || '') === requestId
      )
      const sent = sendCommand('toolbox_force_close_tool', { tool_id: 'ai-collaboration', request_id: requestId })
      if (!sent.ok) throw new Error(sent.message || t.stopFailed)
      const result = await waitResult
      if (result.ok === false) throw new Error(String(result.message || t.stopFailed))
      setStatus('stopped')
    } catch (error) {
      const message = error instanceof Error ? error.message : t.stopFailed
      setStatus('error')
      setErrorMessage(message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="external-collaboration-drawer">
      <section className="ec-section">
        <h4>{ec.description}</h4>
      </section>

      <section className="ec-section">
        <h4>{ec.aiList}</h4>
        <ul className="ec-feature-list">
          <li><strong>ChatGPT</strong> — {ec.coordinator}（固定）</li>
          <li><strong>Gemini</strong> — {ec.searchProvider}、進階搜尋</li>
          <li><strong>Perplexity</strong> — 計算、進階搜尋</li>
          <li><strong>Claude</strong> — 長文本</li>
          <li><strong>DeepSeek</strong> — 推理</li>
          <li><strong>Grok</strong> — 社交媒體、趨勢、即時新聞</li>
        </ul>
        <p className="ec-detail">
          {ec.maxParallel}：6 組並行 · {ec.coordinator}：ChatGPT · 搜尋：Gemini
        </p>
      </section>

      <section className="ec-section">
        <h4>{ec.collaborativeDiagnosis}</h4>
        <ul className="ec-feature-list">
          <li>{ec.groupQuery}</li>
          <li>{ec.sharedMemory}</li>
          <li>{ec.reportExport}</li>
        </ul>
      </section>

      <section className="ec-section ec-status">
        <h4>{ec.status}</h4>
        <div className="ec-status-row">
          <span className={`ec-status-badge ${status}`}>
            {status === 'running' ? ec.statusRunning : status === 'error' ? ec.statusError : ec.statusStopped}
          </span>
          {errorMessage && <span className="ec-error">{errorMessage}</span>}
        </div>
      </section>

      <section className="ec-actions">
        <button
          type="button"
          className={`button button--primary ${busy ? 'is-busy' : ''}`}
          disabled={busy || !connected}
          onClick={handleStart}
        >
          {status === 'running' ? ec.statusRunning : busy ? ec.launching : ec.launch}
        </button>
        <button
          type="button"
          className="button button--secondary"
          disabled={busy || !connected || status !== 'running'}
          onClick={handleStop}
        >
          {busy ? '關閉中…' : t.stop}
        </button>
      </section>

      {!connected && (
        <p className="ec-disconnected">
          後端未連線，無法操作外部協作工具。
        </p>
      )}
    </div>
  )
}