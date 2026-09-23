import { useCallback, useEffect, useState } from 'react'
import { BasePanel } from './BasePanel'
import { mainSystemLocale } from '@/locales/main-system'

const tp = mainSystemLocale.thirdParty

interface ToolVersionInfo {
  tool_id: string
  recorded_version: string
  detected_version: string
  detected: boolean
  path: string
  last_probed_at: string
  error: string
  status: string
  version_matches: boolean
}

interface UpdateCheckResult {
  tool_id: string
  auto_updatable: boolean
  current_version: string
  latest_version: string
  update_available: boolean
  last_checked_at: string
  error: string
}

interface UpdateExecutionResult {
  tool_id: string
  ok: boolean
  before_version: string
  after_version: string
  exit_code: number | null
  executed_at: string
  error: string
  stdout_bytes: number
  stderr_bytes: number
}

interface ThirdPartyStatus {
  ok: boolean
  status?: {
    version: string
    inventory_path: string
    auto_updatable_tools: string[]
    last_full_probe_at: string | null
    last_full_update_check_at: string | null
    versions: Record<string, ToolVersionInfo>
    update_checks: Record<string, UpdateCheckResult>
  }
  live_status?: Record<string, unknown>
}

interface ThirdPartyPanelProps {
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

export function ThirdPartyPanel({
  open,
  onClose,
  sendCommand,
  waitForIpcEvent,
  backendSocket,
}: ThirdPartyPanelProps) {
  return (
    <BasePanel
      open={open}
      onClose={onClose}
      sendCommand={sendCommand}
      waitForIpcEvent={waitForIpcEvent}
      backendSocket={backendSocket}
      title={tp.title}
      eyebrow={tp.eyebrow}
      icon={tp.icon}
    >
      {ThirdPartyPanelContent}
    </BasePanel>
  )
}

function ThirdPartyPanelContent({
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
  const [status, setStatus] = useState<ThirdPartyStatus | null>(null)
  const [loadingState, setLoadingState] = useState<'idle' | 'loading' | 'success' | 'error'>('idle')
  const [errorMsg, setErrorMsg] = useState<string>('')
  const [updatingTool, setUpdatingTool] = useState<string | null>(null)
  const [updateResults, setUpdateResults] = useState<Record<string, UpdateExecutionResult>>({})

  const refreshStatus = useCallback(async () => {
    setLoadingState('loading')
    const result = send('app:get-third-party-status', {})
    if (!result.ok && !result.queued) {
      setLoadingState('error')
      setErrorMsg(result.message || tp.errorLoadStatus)
      return
    }
    try {
      const payload = (await waitForEvent('app:get-third-party-status_result', 10000)) as unknown as ThirdPartyStatus
      if (payload.ok) {
        setStatus(payload)
        setLoadingState('success')
      } else {
        setLoadingState('error')
        setErrorMsg(tp.errorLoadStatus)
      }
    } catch {
      setLoadingState('error')
      setErrorMsg(tp.timeout)
    }
  }, [send, waitForEvent])

  const probeVersions = useCallback(async () => {
    setLoadingState('loading')
    const result = send('app:probe-third-party-versions', {})
    if (!result.ok && !result.queued) {
      setLoadingState('error')
      setErrorMsg(result.message || tp.errorProbeFailed)
      return
    }
    try {
      const payload = (await waitForEvent('app:probe-third-party-versions_result', 60000)) as {
        ok: boolean
        versions: Record<string, ToolVersionInfo>
      }
      if (payload.ok) {
        setStatus((prev) => ({
          ok: true,
          status: {
            ...(prev?.status || {
              version: '',
              inventory_path: '',
              auto_updatable_tools: [],
              last_full_probe_at: null,
              last_full_update_check_at: null,
              versions: {},
              update_checks: {},
            }),
            versions: payload.versions,
            last_full_probe_at: new Date().toISOString(),
          },
        }))
        setLoadingState('success')
      } else {
        setLoadingState('error')
        setErrorMsg(tp.statusLabels.error)
      }
    } catch {
      setLoadingState('error')
      setErrorMsg(tp.timeout)
    }
  }, [send, waitForEvent])

  const checkUpdates = useCallback(async () => {
    setLoadingState('loading')
    const result = send('app:check-third-party-updates', {})
    if (!result.ok && !result.queued) {
      setLoadingState('error')
      setErrorMsg(result.message || tp.errorCheckFailed)
      return
    }
    try {
      const payload = (await waitForEvent('app:check-third-party-updates_result', 120000)) as {
        ok: boolean
        updates: Record<string, UpdateCheckResult>
      }
      if (payload.ok) {
        setStatus((prev) => ({
          ok: true,
          status: {
            ...(prev?.status || {
              version: '',
              inventory_path: '',
              auto_updatable_tools: [],
              last_full_probe_at: null,
              last_full_update_check_at: null,
              versions: {},
              update_checks: {},
            }),
            update_checks: payload.updates,
            last_full_update_check_at: new Date().toISOString(),
          },
        }))
        setLoadingState('success')
      } else {
        setLoadingState('error')
        setErrorMsg(tp.errorCheckFailed)
      }
    } catch {
      setLoadingState('error')
      setErrorMsg(tp.timeout)
    }
  }, [send, waitForEvent])

  const updateTool = useCallback(
    async (toolId: string) => {
      setUpdatingTool(toolId)
      try {
        const result = send('app:update-third-party-tool', {
          tool_id: toolId,
          approval_token: 'governance-auto-approve',
        })
        if (!result.ok && !result.queued) {
          setErrorMsg(result.message || tp.errorUpdateFailed)
          return
        }
        const payload = (await waitForEvent('app:update-third-party-tool_result', 120000)) as UpdateExecutionResult
        setUpdateResults((prev) => ({ ...prev, [toolId]: payload }))
        void probeVersions()
      } catch {
        setErrorMsg(tp.timeout)
      } finally {
        setUpdatingTool(null)
      }
    },
    [send, waitForEvent, probeVersions]
  )

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  const versions = status?.status?.versions || {}
  const updateChecks = status?.status?.update_checks || {}
  const autoUpdatable = status?.status?.auto_updatable_tools || []
  const toolIds = Object.keys(versions).sort()

  return (
    <>
      <section className='base-panel-section'>
        <div className='base-panel-section__header'>
          <h4 className='base-panel-section__title-text'>{tp.title}</h4>
          <p>{tp.subtitle}</p>
        </div>
        <div className='base-panel-actions'>
          <button
            className='base-panel-btn base-panel-btn--secondary'
            onClick={() => void probeVersions()}
            disabled={loadingState === 'loading' || state.loading}
          >
            {tp.refreshVersions}
          </button>
          <button
            className='base-panel-btn base-panel-btn--secondary'
            onClick={() => void checkUpdates()}
            disabled={loadingState === 'loading' || state.loading}
          >
            {tp.checkUpdates}
          </button>
          <button
            className='base-panel-btn base-panel-btn--primary'
            onClick={() => void refreshStatus()}
            disabled={loadingState === 'loading' || state.loading}
          >
            {tp.refresh}
          </button>
        </div>
      </section>

      {errorMsg && (
        <div className='base-panel__error' role='alert'>
          {errorMsg}
          <button type='button' className='base-panel__error-dismiss' onClick={() => {}} aria-label={tp.errorDismiss}>
            <svg width='14' height='14' viewBox='0 0 14 14' fill='none'>
              <path d='M4 4L10 10M10 4L4 10' stroke='currentColor' strokeWidth='1.5' strokeLinecap='round' />
            </svg>
          </button>
        </div>
      )}

      {status?.status && (
        <section className='base-panel-section'>
          <div className='base-panel-meta'>
            <div className='base-panel-meta__item'>
              <span className='base-panel-meta__label'>{tp.serviceVersion}</span>
              <strong className='base-panel-meta__value'>{status.status.version}</strong>
            </div>
            <div className='base-panel-meta__item'>
              <span className='base-panel-meta__label'>{tp.autoUpdatable}</span>
              <strong className='base-panel-meta__value'>{autoUpdatable.join(', ') || '—'}</strong>
            </div>
            {status.status.last_full_probe_at && (
              <div className='base-panel-meta__item'>
                <span className='base-panel-meta__label'>{tp.lastProbe}</span>
                <strong className='base-panel-meta__value base-panel-meta__time'>
                  {new Date(status.status.last_full_probe_at).toLocaleString('zh-TW', { hour12: false })}
                </strong>
              </div>
            )}
          </div>
        </section>
      )}

      <div className='base-panel-table-wrap'>
        <table className='base-panel-table'>
          <thead>
            <tr>
              <th>{tp.tool}</th>
              <th>記錄版本</th>
              <th>{tp.detectedVersion}</th>
              <th>{tp.status}</th>
              <th>{tp.latestVersion}</th>
              <th>{tp.updatable}</th>
              <th>{tp.action}</th>
            </tr>
          </thead>
          <tbody>
            {toolIds.length === 0 && (
              <tr>
                <td colSpan={7} className='base-panel-empty'>
                  {loadingState === 'loading' ? tp.loading : tp.empty}
                </td>
              </tr>
            )}
            {toolIds.map((toolId) => {
              const info = versions[toolId]
              const update = updateChecks[toolId]
              const canUpdate = autoUpdatable.includes(toolId)
              const result = updateResults[toolId]
              const isUpdating = updatingTool === toolId
              const statusKey = info?.status || ''
              const statusLabels = tp.statusLabels as Record<string, string>
              const tone = statusLabels[statusKey] ? 'success' : 'muted'
              return (
                <tr key={toolId}>
                  <td className='base-panel-table__tool-id'>{toolId}</td>
                  <td className='base-panel-table__version'>{info?.recorded_version || '—'}</td>
                  <td className='base-panel-table__version'>{info?.detected_version || '—'}</td>
                  <td>
                    <span className={'base-panel-status tp-status--' + tone}>
                      {statusLabels[statusKey] || info?.status || '—'}
                    </span>
                  </td>
                  <td className='base-panel-table__version'>{update?.latest_version || '—'}</td>
                  <td>
                    {canUpdate ? (
                      <span className='base-panel-badge base-panel-badge--yes'>{tp.autoUpdatableBadge}</span>
                    ) : (
                      <span className='base-panel-badge base-panel-badge--no'>{tp.manualBadge}</span>
                    )}
                  </td>
                  <td>
                    {canUpdate && (
                      <button
                        className='base-panel-btn base-panel-btn--small base-panel-btn--update'
                        onClick={() => void updateTool(toolId)}
                        disabled={isUpdating}
                      >
                        {isUpdating ? tp.updating : tp.updateBtn}
                      </button>
                    )}
                    {result && (
                      <span className={'base-panel-update-result ' + (result.ok ? 'base-panel-update-result--ok' : 'base-panel-update-result--fail')}>
                        {result.ok
                          ? '✓ ' + (result.after_version || tp.updated)
                          : '✗ ' + (result.error || tp.errorUpdateFailed)}
                      </span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

export { ThirdPartyPanel as default }
