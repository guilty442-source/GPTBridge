import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  serviceManager,
  startStartupPipeline,
} from '@/services/RuntimeServiceManager'
import { useBackendSocket } from '@/hooks/useBackendSocket'
import { ToolboxEntry } from '@/ui/toolbox/ToolboxEntry'
import { useToolboxApplications } from '@/ui/toolbox/useToolboxApplications'
import {
  SovereignDashboard,
  type RuntimeStatusPayload,
} from '@/ui/sovereign/SovereignDashboard'
import { Drawer } from '@/ui/drawer/Drawer'
import { ThirdPartyPanel } from '@/ui/third-party/ThirdPartyPanel'
import { formatBytes, formatProjectSize } from '@/shared/utils/format'
import { mainSystemLocale } from '@/locales/main-system'
import '../App.css'

const UI_ZOOM_STORAGE_KEY = 'gptbridge_ui_zoom_factor'
const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3
const t = mainSystemLocale.product
const xr = mainSystemLocale.xingchengReport

type SystemMetrics = {
  diskUsagePercent?: number | null
  diskTotalBytes?: number | null
  diskFreeBytes?: number | null
  diskRoot?: string
}

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

function displayVersion(value: string): string {
  const match = /^(\d+)\.(\d+)(?:\.\d+)?$/.exec(value.trim())
  return match ? `${match[1]}.${match[2]}` : '1.0'
}

function formatFileCount(value: number | undefined): string {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? `${new Intl.NumberFormat('zh-TW').format(value)} ${t.files}`
    : t.pendingCheck
}

function capacityDetail(bytes: number | undefined, fileCount: number | undefined): string {
  return `${formatBytes(bytes, { exactBytes: true, fallback: t.pendingCheck })} · ${formatFileCount(fileCount)}`
}

export default function App() {
  const [appVersion, setAppVersion] = useState('1.0.0')
  const [maintenanceReady, setMaintenanceReady] = useState(false)
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatusPayload>({})
  const [systemMetrics, setSystemMetrics] = useState<SystemMetrics>({})
  const [drawerSovereign, setDrawerSovereign] = useState(false)
  const [drawerXingcheng, setDrawerXingcheng] = useState(false)
  const [drawerCapacity, setDrawerCapacity] = useState(false)
  const [drawerThirdParty, setDrawerThirdParty] = useState(false)
  const backendSocket = useBackendSocket()
  const sendCommand = backendSocket.sendCommand
  const connected = backendSocket.status === 'Connected'
  const operational = connected && maintenanceReady

  const waitForIpcEvent = useCallback(
    (
      eventName: string,
      timeoutMs: number,
      predicate?: (payload: Record<string, unknown>) => boolean
    ): Promise<Record<string, unknown>> => {
      return new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => {
          window.removeEventListener('ipc_event', handler)
          reject(new Error(`${t.backendResponseTimeout}：${eventName}`))
        }, timeoutMs)

        const handler = (event: Event) => {
          const customEvent = event as CustomEvent
          const detail = customEvent.detail || {}
          if (detail.event !== eventName) return
          const payload = (detail.payload || {}) as Record<string, unknown>
          if (predicate && !predicate(payload)) return
          window.clearTimeout(timer)
          window.removeEventListener('ipc_event', handler)
          resolve(payload)
        }

        window.addEventListener('ipc_event', handler)
      })
    },
    []
  )

  const {
    toolboxTools,
    toolboxSyncing,
    toolboxSyncedAt,
    mainSystemSizeBytes,
    mainSystemFileCount,
    dependencySizeBytes,
    dependencyFileCount,
    sharedLayerSizeBytes,
    sharedLayerFileCount,
    workspaceSizeBytes,
    workspaceFileCount,
    refreshToolboxTools,
    handleToolboxAction,
  } = useToolboxApplications({
    backendStatus: backendSocket.status,
    sendCommand,
    waitForIpcEvent,
  })

  useEffect(() => {
    const api = window.electron
    if (!api?.invoke) return

    let disposed = false

    // Initial one-shot fetch for version + system metrics
    const refreshStatus = async () => {
      try {
        const status = (await api.invoke('app:get-status')) as {
          version?: unknown
          systemMetrics?: SystemMetrics
        } | null
        if (disposed) return
        const version = String(status?.version ?? '').trim()
        if (version) setAppVersion(version)
        if (status?.systemMetrics) setSystemMetrics(status.systemMetrics)
      } catch {
        // Keep the last successful system sample on a transient IPC failure.
      }
    }

    void refreshStatus()

    // Subscribe to real-time runtime_status_push events (replaces 5s polling)
    const onStatusPush = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== 'runtime_status_push') return
      const payload = (detail.payload || {}) as Record<string, unknown>
      const version = String(payload.version ?? '').trim()
      if (version) setAppVersion(version)
      const metrics = payload.systemMetrics as SystemMetrics | undefined
      if (metrics) setSystemMetrics(metrics)
    }
    window.addEventListener('ipc_event', onStatusPush)

    const saved = (() => {
      try {
        const raw = window.localStorage.getItem(UI_ZOOM_STORAGE_KEY)
        const parsed = raw ? Number(raw) : 1
        if (Number.isNaN(parsed) || parsed <= 0) return 1
        return clampUiZoom(parsed)
      } catch {
        return 1
      }
    })()

    void api.invoke('app:set-ui-zoom', { factor: saved })

    return () => {
      disposed = true
      window.removeEventListener('ipc_event', onStatusPush)
    }
  }, [])

  useEffect(() => {
    if (serviceManager.getAllStates().length === 0) {
      void startStartupPipeline()
    }
  }, [])

  useEffect(() => {
    let disposed = false

    if (!connected) {
      setMaintenanceReady(false)
      return () => undefined
    }

    // Subscribe to real-time status pushes
    const onStatusPush = (event: Event) => {
      if (disposed) return
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      // Accept both the push event and the command result
      if (
        detail.event !== 'runtime_status_push' &&
        detail.event !== 'app:get-runtime-status_result'
      ) {
        return
      }
      const payload = (detail.payload || {}) as Record<string, unknown>
      const ready = payload.maintenance_ready === true
      setMaintenanceReady(ready)
      setRuntimeStatus(payload as RuntimeStatusPayload)
    }
    window.addEventListener('ipc_event', onStatusPush)

    // Register the listener before requesting the initial snapshot. A local
    // backend can answer in the same event-loop turn; requesting first could
    // lose that response and leave the UI showing a false channel anomaly.
    const sent = sendCommand('app:get-runtime-status', {
      source: 'product_readiness_gate',
    })
    if (!sent.ok) {
      setMaintenanceReady(false)
    }

    return () => {
      disposed = true
      window.removeEventListener('ipc_event', onStatusPush)
    }
  }, [connected, sendCommand])

  const summary = useMemo(() => {
    const running = toolboxTools.filter((tool) => tool.status === 'running').length
    const issues = toolboxTools.filter(
      (tool) =>
        tool.status === 'error' ||
        (tool.launchable !== false && tool.runtimeAvailable === false)
    ).length
    return { running, issues, total: toolboxTools.length }
  }, [toolboxTools])
  const xingchengReview = useMemo(() => {
    if (backendSocket.status === 'Connecting' || backendSocket.status === 'Repairing') {
      return { tone: 'warning' as const, state: xr.reviewing, detail: xr.reviewingDetail, issues: [{ id: 'backend-reconnecting', source: xr.informationLayer, title: xr.backendInterrupted, detail: `${xr.reviewingDetail}：${backendSocket.status}`, status: xr.autoRepairing }] }
    }
    if (!connected) {
      return { tone: 'warning' as const, state: xr.connectionAnomaly, detail: xr.connectionDetail, issues: [{ id: 'backend-offline', source: xr.informationLayer, title: xr.backendDisconnected, detail: xr.backendDisconnectedDetail, status: xr.waitingRecovery }] }
    }
    if (!maintenanceReady) {
      return { tone: 'warning' as const, state: xr.healthAnomaly, detail: xr.healthDetail, issues: [{ id: 'maintenance-not-ready', source: xr.maintenanceSovereign, title: xr.maintenanceNotReady, detail: xr.maintenanceNotReadyDetail, status: xr.monitoring }] }
    }
    const affected = toolboxTools
      .filter((tool) => tool.status === 'error' || (tool.launchable !== false && tool.runtimeAvailable === false))
    const issues = affected.map((tool) => ({
      id: `tool-${tool.id}`,
      source: tool.name,
      title: tool.status === 'error' ? xr.toolError : xr.runtimeUnavailable,
      detail: tool.note || tool.description || tool.summary || xr.noFurtherDetail,
      status: tool.status === 'error' ? xr.needsAction : xr.waitingRuntime,
    }))
    if (affected.length > 0) {
      return {
        tone: 'warning' as const,
        state: `${affected.length} ${xr.affectedSuffix}`,
        detail: affected.slice(0, 3).map((tool) => tool.name).join('、'),
        issues,
      }
    }
    return { tone: 'ok' as const, state: xr.normal, detail: xr.normalDetail, issues: [] }
  }, [backendSocket.status, connected, maintenanceReady, toolboxTools])
  // The header indicator reports transport state only. Tool or maintenance
  // findings remain in the review panel and must not falsely mark a healthy
  // frontend/backend channel as offline.
  const connection = connected
    ? { label: xr.systemNormal, detail: xr.normalDetail, tone: 'online' as const }
    : backendSocket.status === 'Connecting' || backendSocket.status === 'Repairing' || backendSocket.status === 'Synchronizing'
      ? { label: xr.systemReviewing, detail: xr.reviewingDetail, tone: 'pending' as const }
      : { label: xr.systemAnomaly, detail: xr.connectionDetail, tone: 'offline' as const }
  const diskUsedBytes =
    typeof systemMetrics.diskTotalBytes === 'number' &&
    typeof systemMetrics.diskFreeBytes === 'number'
      ? Math.max(0, systemMetrics.diskTotalBytes - systemMetrics.diskFreeBytes)
      : null

  return (
    <div className="product-shell">
      <header className="product-header">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">G</div>
          <div>
            <strong>GPTBridge</strong>
            <span>{t.brandSubtitle}</span>
          </div>
        </div>

        <div className="header-controls">
          <div
            className="connection-indicator"
            data-tone={connection.tone}
            data-testid="backend-connection"
          >
            <span className="connection-indicator__dot" />
            <span>
              <strong>{connection.label}</strong>
              <small>{connection.detail}</small>
            </span>
          </div>
          <span className="version-badge" data-testid="product-version">
            v{displayVersion(appVersion)}
          </span>
        </div>
      </header>

      <main className="product-main">
        {!operational && (
          <aside className="connection-notice" role="status">
            <strong>{connected ? t.maintenanceIncomplete : t.offlineSafeMode}</strong>
            <span>
              {connected
                ? t.maintenanceNotice
                : t.offlineNotice}
            </span>
          </aside>
        )}

        {/* Operational summary */}
        <section className="hero-grid" aria-label={t.systemOverview}>
          <article className="hero-card hero-card--primary">
            <span className="hero-card__label">{t.availableTools}</span>
            <strong className="hero-card__value">{summary.total}</strong>
            <small className="hero-card__hint">{t.availableToolsHint}</small>
          </article>
          <article className="hero-card">
            <span className="hero-card__label">{t.runningTools}</span>
            <strong className="hero-card__value">{summary.running}</strong>
            <small className="hero-card__hint">{t.runningToolsHint}</small>
          </article>
          <article className="hero-card" data-tone={summary.issues > 0 ? 'warning' : 'ok'}>
            <span className="hero-card__label">{t.issues}</span>
            <strong className="hero-card__value">{summary.issues}</strong>
            <small className="hero-card__hint">{t.issuesHint}</small>
          </article>
          <article className="hero-card">
            <span className="hero-card__label">{t.commandStrategy}</span>
            <strong className="hero-card__value hero-card__value--text">{t.requestToolExecution}</strong>
            <small className="hero-card__hint">{t.commandStrategyHint}</small>
          </article>
          <button
            type="button"
            className="hero-card hero-card--interactive"
            data-tone={xingchengReview.tone}
            data-testid="xingcheng-global-review"
            aria-haspopup="dialog"
            onClick={() => setDrawerXingcheng(true)}
          >
            <span className="hero-card__label">{mainSystemLocale.sovereign.xingchengTitle}</span>
            <strong className="hero-card__value hero-card__value--text">{xingchengReview.state}</strong>
            <small className="hero-card__hint">{xingchengReview.detail}</small>
            <span className="hero-card__action">{xr.viewDetails}</span>
          </button>
        </section>

        <div className="section-heading">
          <div>
            <span className="eyebrow">系統管理</span>
            <h2>常用管理入口</h2>
          </div>
          <span>詳細設定不干擾日常工具操作</span>
        </div>

        {/* Drawer trigger row */}
        <section className="drawer-triggers">
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerSovereign(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">S</span>
            <span className="drawer-trigger__text">
              <strong>{mainSystemLocale.sovereign.title}</strong>
              <small>{mainSystemLocale.sovereign.eyebrow}</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerCapacity(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">D</span>
            <span className="drawer-trigger__text">
              <strong>{t.capacityDetails}</strong>
              <small>{t.systemDisk} · {t.workspaceSize}</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerThirdParty(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">T</span>
            <span className="drawer-trigger__text">
              <strong>第三方軟體管理</strong>
              <small>版本探測 · 自動更新</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </section>

        <ToolboxEntry
          tools={toolboxTools}
          connected={operational}
          syncing={toolboxSyncing}
          syncedAt={toolboxSyncedAt}
          onRefresh={() => void refreshToolboxTools()}
          onToolAction={handleToolboxAction}
        />
      </main>

      <footer className="product-footer">
        <span>GPTBridge v{displayVersion(appVersion)}</span>
        <span>{t.footerPlatform}</span>
      </footer>

      <Drawer
        open={drawerXingcheng}
        onClose={() => setDrawerXingcheng(false)}
        title={xr.title}
        eyebrow={xr.eyebrow}
        icon="星"
      >
        <div className="xingcheng-report" data-testid="xingcheng-report-detail">
          <section className="xingcheng-report__summary" data-tone={xingchengReview.tone}>
            <span>{xr.currentDecision}</span>
            <strong>{xingchengReview.state}</strong>
            <p>{xingchengReview.detail}</p>
          </section>
          {xingchengReview.issues.length > 0 ? (
            <div className="xingcheng-report__issues">
              {xingchengReview.issues.map((issue) => (
                <article className="xingcheng-issue" key={issue.id}>
                  <div className="xingcheng-issue__head">
                    <strong>{issue.title}</strong>
                    <span>{issue.status}</span>
                  </div>
                  <dl>
                    <div><dt>{xr.source}</dt><dd>{issue.source}</dd></div>
                    <div><dt>{xr.details}</dt><dd>{issue.detail}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          ) : (
            <p className="xingcheng-report__empty">{xr.empty}</p>
          )}
        </div>
      </Drawer>

      {/* Sovereign drawer */}
      <Drawer
        open={drawerSovereign}
        onClose={() => setDrawerSovereign(false)}
        title={mainSystemLocale.sovereign.title}
        eyebrow={mainSystemLocale.sovereign.eyebrow}
        icon="S"
      >
        <SovereignDashboard runtimeStatus={runtimeStatus} />
      </Drawer>

      {/* Third-party management drawer */}
      <Drawer
        open={drawerThirdParty}
        onClose={() => setDrawerThirdParty(false)}
        title="第三方軟體管理"
        eyebrow="版本探測 · 自動更新"
        icon="T"
      >
        <ThirdPartyPanel />
      </Drawer>

      {/* Capacity drawer */}
      <Drawer
        open={drawerCapacity}
        onClose={() => setDrawerCapacity(false)}
        title={t.capacityDetails}
        eyebrow={t.systemOverview}
        icon="D"
      >
        <div className="capacity-drawer">
          <article className="capacity-row" data-testid="system-disk-size">
            <div className="capacity-row__head">
              <strong>{t.systemDisk} {systemMetrics.diskRoot || ''}</strong>
              <span className="capacity-row__big">
                {formatBytes(systemMetrics.diskTotalBytes, { exactBytes: true, fallback: t.pendingCheck })}
              </span>
            </div>
            <p className="capacity-row__detail">
              {t.usageRate}{' '}
              {typeof systemMetrics.diskUsagePercent === 'number'
                ? `${systemMetrics.diskUsagePercent.toFixed(1)}%`
                : t.pendingCheck}
              {' · '}
              {t.used} {formatBytes(diskUsedBytes, { exactBytes: true, fallback: t.pendingCheck })}
              {' · '}
              {t.available} {formatBytes(systemMetrics.diskFreeBytes, { exactBytes: true, fallback: t.pendingCheck })}
            </p>
          </article>

          <article className="capacity-row" data-testid="main-system-folder-size">
            <div className="capacity-row__head">
              <strong>{t.mainSystemSize}</strong>
              <span className="capacity-row__big">
                {formatProjectSize(mainSystemSizeBytes, { fallback: t.pendingCheck })}
              </span>
            </div>
            <p className="capacity-row__detail">
              {t.mainSystemSizeHint} · {capacityDetail(mainSystemSizeBytes, mainSystemFileCount)}
            </p>
          </article>

          <article className="capacity-row" data-testid="dependency-folder-size">
            <div className="capacity-row__head">
              <strong>{t.dependencySize}</strong>
              <span className="capacity-row__big">
                {formatProjectSize(dependencySizeBytes, { fallback: t.pendingCheck })}
              </span>
            </div>
            <p className="capacity-row__detail">
              {t.dependencySizeHint} · {capacityDetail(dependencySizeBytes, dependencyFileCount)}
            </p>
          </article>

          <article className="capacity-row" data-testid="shared-layer-folder-size">
            <div className="capacity-row__head">
              <strong>{t.sharedLayerSize}</strong>
              <span className="capacity-row__big">
                {formatProjectSize(sharedLayerSizeBytes, { fallback: t.pendingCheck })}
              </span>
            </div>
            <p className="capacity-row__detail">
              {t.sharedLayerSizeHint} · {capacityDetail(sharedLayerSizeBytes, sharedLayerFileCount)}
            </p>
          </article>

          <article className="capacity-row" data-testid="workspace-folder-size">
            <div className="capacity-row__head">
              <strong>{t.workspaceSize}</strong>
              <span className="capacity-row__big">
                {formatProjectSize(workspaceSizeBytes, { fallback: t.pendingCheck })}
              </span>
            </div>
            <p className="capacity-row__detail">
              {t.workspaceSizeHint} · {capacityDetail(workspaceSizeBytes, workspaceFileCount)}
            </p>
          </article>
        </div>
      </Drawer>
    </div>
  )
}
