import { useCallback, useEffect, useState } from 'react'
import { useBackendSocket } from '@/hooks/useBackendSocket'
import { mainSystemLocale } from '@/locales/main-system'
import './third-party.css'

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

type LoadingState = 'idle' | 'loading' | 'success' | 'error'

const tp = mainSystemLocale.thirdParty

export function ThirdPartyPanel() {
  const { sendCommand } = useBackendSocket()
  const [status, setStatus] = useState<ThirdPartyStatus | null>(null)
  const [loadingState, setLoadingState] = useState<LoadingState>('idle')
  const [errorMsg, setErrorMsg] = useState('')
  const [updatingTool, setUpdatingTool] = useState<string | null>(null)
  const [updateResults, setUpdateResults] = useState<Record<string, UpdateExecutionResult>>({})

  const refreshStatus = useCallback(async () => {
    setLoadingState('loading')
    const result = sendCommand('app:get-third-party-status', {})
    if (!result.ok) {
      setLoadingState('error')
      setErrorMsg(result.message || tp.errorFetchStatus)
      return
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (detail.event !== 'app:get-third-party-status_result') return
      window.removeEventListener('ipc_event', handler)
      const payload = detail.payload as ThirdPartyStatus
      if (payload.ok) {
        setStatus(payload)
        setLoadingState('success')
      } else {
        setLoadingState('error')
        setErrorMsg(payload.status ? tp.errorLoadStatus : tp.errorFetchStatus)
      }
    }
    window.addEventListener('ipc_event', handler)
    setTimeout(() => {
      window.removeEventListener('ipc_event', handler)
      if (loadingState === 'loading') {
        setLoadingState('error')
        setErrorMsg(tp.timeout)
      }
    }, 10000)
  }, [sendCommand])

  const probeVersions = useCallback(async () => {
    setLoadingState('loading')
    const result = sendCommand('app:probe-third-party-versions', {})
    if (!result.ok) {
      setLoadingState('error')
      setErrorMsg(result.message || tp.errorProbeVersions)
      return
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (detail.event !== 'app:probe-third-party-versions_result') return
      window.removeEventListener('ipc_event', handler)
      const payload = detail.payload as {
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
        setErrorMsg(tp.errorProbeFailed)
      }
    }
    window.addEventListener('ipc_event', handler)
  }, [sendCommand])

  const checkUpdates = useCallback(async () => {
    setLoadingState('loading')
    const result = sendCommand('app:check-third-party-updates', {})
    if (!result.ok) {
      setLoadingState('error')
      setErrorMsg(result.message || tp.errorCheckUpdates)
      return
    }
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (detail.event !== 'app:check-third-party-updates_result') return
      window.removeEventListener('ipc_event', handler)
      const payload = detail.payload as {
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
    }
    window.addEventListener('ipc_event', handler)
  }, [sendCommand])

  const updateTool = useCallback(
    async (toolId: string) => {
      setUpdatingTool(toolId)
      const result = sendCommand('app:update-third-party-tool', {
        tool_id: toolId,
        approval_token: 'governance-auto-approve',
      })
      if (!result.ok && !result.queued) {
        setUpdatingTool(null)
        setErrorMsg(result.message || tp.errorUpdate)
        return
      }
      const handler = (event: Event) => {
        const detail = (event as CustomEvent).detail
        if (detail.event !== 'app:update-third-party-tool_result') return
        window.removeEventListener('ipc_event', handler)
        const payload = detail.payload as UpdateExecutionResult
        setUpdateResults((prev) => ({ ...prev, [toolId]: payload }))
        setUpdatingTool(null)
        void probeVersions()
      }
      window.addEventListener('ipc_event', handler)
    },
    [sendCommand, probeVersions]
  )

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  const versions = status?.status?.versions || {}
  const updateChecks = status?.status?.update_checks || {}
  const autoUpdatable = status?.status?.auto_updatable_tools || []
  const toolIds = Object.keys(versions).sort()

  return (
    <section className="third-party-panel" aria-labelledby="third-party-title">
      <header className="third-party-header">
        <div>
          <span className="eyebrow">{tp.eyebrow}</span>
          <h2 id="third-party-title">{tp.title}</h2>
          <p>{tp.subtitle}</p>
        </div>
        <div className="third-party-actions">
          <button
            className="tp-btn tp-btn--secondary"
            onClick={() => void probeVersions()}
            disabled={loadingState === 'loading'}
          >
            {tp.refreshVersions}
          </button>
          <button
            className="tp-btn tp-btn--secondary"
            onClick={() => void checkUpdates()}
            disabled={loadingState === 'loading'}
          >
            {tp.checkUpdates}
          </button>
          <button
            className="tp-btn tp-btn--primary"
            onClick={() => void refreshStatus()}
            disabled={loadingState === 'loading'}
          >
            {tp.refresh}
          </button>
        </div>
      </header>

      {errorMsg && (
        <div className="tp-error-banner" role="alert">
          {errorMsg}
        </div>
      )}

      {status?.status && (
        <div className="tp-meta">
          <div className="tp-meta__item">
            <span>{tp.serviceVersion}</span>
            <strong>{status.status.version}</strong>
          </div>
          <div className="tp-meta__item">
            <span>{tp.autoUpdatable}</span>
            <strong>{autoUpdatable.join(', ') || '—'}</strong>
          </div>
          {status.status.last_full_probe_at && (
            <div className="tp-meta__item">
              <span>{tp.lastProbe}</span>
              <strong className="tp-meta__time">
                {new Date(status.status.last_full_probe_at).toLocaleString('zh-TW', {
                  hour12: false,
                })}
              </strong>
            </div>
          )}
        </div>
      )}

      <div className="tp-table-wrap">
        <table className="tp-table">
          <thead>
            <tr>
              <th>{tp.tool}</th>
              <th>{tp.recordedVersion}</th>
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
                <td colSpan={7} className="tp-empty">
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
                  <td className="tp-tool-id">{toolId}</td>
                  <td className="tp-version">{info?.recorded_version || '—'}</td>
                  <td className="tp-version">{info?.detected_version || '—'}</td>
                  <td>
                    <span className={`tp-status tp-status--${tone}`}>
                      {statusLabels[statusKey] || info?.status || '—'}
                    </span>
                  </td>
                  <td className="tp-version">{update?.latest_version || '—'}</td>
                  <td>
                    {canUpdate ? (
                      <span className="tp-badge tp-badge--yes">{tp.autoUpdatableBadge}</span>
                    ) : (
                      <span className="tp-badge tp-badge--no">{tp.manualBadge}</span>
                    )}
                  </td>
                  <td>
                    {canUpdate && (
                      <button
                        className="tp-btn tp-btn--small tp-btn--update"
                        onClick={() => void updateTool(toolId)}
                        disabled={isUpdating}
                      >
                        {isUpdating ? tp.updating : tp.updateBtn}
                      </button>
                    )}
                    {result && (
                      <span
                        className={`tp-update-result ${result.ok ? 'tp-update-result--ok' : 'tp-update-result--fail'}`}
                      >
                        {result.ok
                          ? `✓ ${result.after_version || tp.updated}`
                          : `✗ ${result.error || tp.failed}`}
                      </span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}