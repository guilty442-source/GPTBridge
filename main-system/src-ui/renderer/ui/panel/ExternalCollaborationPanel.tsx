import { useCallback, useState, useEffect } from 'react'
import { BasePanel } from './BasePanel'
import { mainSystemLocale } from '@/locales/main-system'

const ec = mainSystemLocale.externalCollaboration
const t = mainSystemLocale.toolbox

const COLLABORATION_ACTION_TIMEOUT_MS = 120000

interface ExternalCollaborationPanelProps {
  open: boolean
  onClose: () => void
  sendCommand: (command: string, payload?: unknown) => { ok: boolean; queued: boolean; message?: string }
  waitForIpcEvent: (
    eventName: string,
    timeoutMs: number,
    predicate?: (payload: Record<string, unknown>) => boolean
  ) => Promise<Record<string, unknown>>
  backendSocket: { status: string }
}

export function ExternalCollaborationPanel({
  open,
  onClose,
  sendCommand,
  waitForIpcEvent,
  backendSocket,
}: ExternalCollaborationPanelProps) {
  return (
    <BasePanel
      open={open}
      onClose={onClose}
      sendCommand={sendCommand}
      waitForIpcEvent={waitForIpcEvent}
      backendSocket={backendSocket}
      title={ec.title}
      eyebrow={ec.eyebrow}
      icon={ec.icon}
    >
      {ExternalCollaborationPanelContent}
    </BasePanel>
  )
}

function ExternalCollaborationPanelContent({
  state,
  locale,
  send,
  waitForEvent,
  generateRequestId,
  setLoading,
  setError,
  clearError,
  connected,
}: {
  state: any
  locale: any
  send: any
  waitForEvent: any
  generateRequestId: any
  setLoading: any
  setError: any
  clearError: any
  connected: boolean
}) {
  const [status, setStatus] = useState<'stopped' | 'running' | 'error'>('stopped')
  const [busy, setBusy] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string>('')

  const handleStart = useCallback(async () => {
    if (busy || !connected) return
    setBusy(true)
    clearError()
    setStatus('running')
    const requestId = generateRequestId('ai-collaboration:start')
    try {
      const waitResult = waitForEvent('toolbox_start_tool_result', COLLABORATION_ACTION_TIMEOUT_MS, (payload: Record<string, unknown>) =>
        String(payload.request_id || '') === requestId
      )
      const sent = send('toolbox_start_tool', { tool_id: 'ai-collaboration', request_id: requestId })
      if (!sent.ok) throw new Error(sent.message || locale.launch)
      const result = await waitResult
      if (result.ok === false) throw new Error(String(result.message || locale.launch))
      setStatus('running')
    } catch (error) {
      const message = error instanceof Error ? error.message : locale.launch
      setStatus('error')
      setErrorMessage(message)
    } finally {
      setBusy(false)
    }
  }, [busy, connected, send, waitForEvent, generateRequestId])

  const handleStop = useCallback(async () => {
    if (busy || !connected) return
    setBusy(true)
    clearError()
    const requestId = generateRequestId('ai-collaboration:stop')
    try {
      const waitResult = waitForEvent('toolbox_force_close_tool_result', COLLABORATION_ACTION_TIMEOUT_MS, (payload: Record<string, unknown>) =>
        String(payload.request_id || '') === requestId
      )
      const sent = send('toolbox_force_close_tool', { tool_id: 'ai-collaboration', request_id: requestId })
      if (!sent.ok) throw new Error(sent.message || locale.stop)
      const result = await waitResult
      if (result.ok === false) throw new Error(String(result.message || locale.stop))
      setStatus('stopped')
    } catch (error) {
      const message = error instanceof Error ? error.message : locale.stop
      setStatus('error')
      setErrorMessage(message)
    } finally {
      setBusy(false)
    }
  }, [busy, connected, send, waitForEvent, generateRequestId])

  return (
    <>
      <section className="base-panel-section">
        <h4 className="base-panel-section__title-text">{ec.description}</h4>
      </section>

      <section className="base-panel-section">
        <h4 className="base-panel-section__title-text">{ec.aiList}</h4>
        <ul className="base-panel-feature-list">
          <li><strong>{ec.ai01}</strong> — {ec.coordinator}{ec.coordinatorFixed}</li>
          <li><strong>{ec.ai02}</strong> — {ec.searchProvider}{ec.searchProviderAdvanced}</li>
          <li><strong>{ec.ai03}</strong> — {ec.calculationAdvancedSearch}</li>
          <li><strong>{ec.ai04}</strong> — {ec.longText}</li>
          <li><strong>{ec.ai05}</strong> — {ec.reasoning}</li>
          <li><strong>{ec.ai06}</strong> — {ec.socialMediaTrendsNews}</li>
        </ul>
        <p className="base-panel-detail">
          {ec.detailMaxParallel}
        </p>
      </section>

      <section className="base-panel-section">
        <h4 className="base-panel-section__title-text">{ec.collaborativeDiagnosis}</h4>
        <ul className="base-panel-feature-list">
          <li>{ec.groupQuery}</li>
          <li>{ec.sharedMemory}</li>
          <li>{ec.reportExport}</li>
        </ul>
      </section>

      <section className="base-panel-section">
        <div className="base-panel-section__header">
          <h4 className="base-panel-section__title-text">{ec.status}</h4>
        </div>
        <div className="base-panel-status-row">
          <span className={`base-panel-status-badge ${status}`}>
            {status === 'running' ? ec.statusRunning : status === 'error' ? ec.statusError : ec.statusStopped}
          </span>
          {errorMessage && <span className="base-panel-error">{errorMessage}</span>}
        </div>
      </section>

      <section className="base-panel-section base-panel-actions">
        <button
          type="button"
          className={`base-panel-btn base-panel-btn--primary ${busy ? 'base-panel-btn--loading' : ''}`}
          disabled={busy || !connected}
          onClick={handleStart}
        >
          {status === 'running' ? ec.statusRunning : busy ? ec.launching : ec.launch}
        </button>
        <button
          type="button"
          className="base-panel-btn base-panel-btn--secondary"
          disabled={busy || !connected || status !== 'running'}
          onClick={handleStop}
        >
          {busy ? locale.closing : locale.stop}
        </button>
      </section>

      {!connected && (
        <div className="base-panel__disconnected">
          {locale.disconnected}
        </div>
      )}
    </>
  )
}

export { ExternalCollaborationPanel as default }