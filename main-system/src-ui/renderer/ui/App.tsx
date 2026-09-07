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

function connectionCopy(
  status: string,
  maintenanceReady: boolean
): {
  label: string
  detail: string
  tone: 'online' | 'pending' | 'offline'
} {
  if (status === 'Connected' && maintenanceReady) {
    return {
      label: t.backendConnected,
      detail: t.backendConnectedDetail,
      tone: 'online',
    }
  }
  if (status === 'Connected') {
    return {
      label: t.maintenanceInProgress,
      detail: t.maintenanceInProgressDetail,
      tone: 'pending',
    }
  }
  if (status === 'Connecting' || status === 'Repairing') {
    return {
      label: status === 'Repairing' ? t.autoRepairing : t.connecting,
      detail: t.connectingDetail,
      tone: 'pending',
    }
  }
  return {
    label: t.backendDisconnected,
    detail: t.backendDisconnectedDetail,
    tone: 'offline',
  }
}

export default function App() {
  const [appVersion, setAppVersion] = useState('1.0.0')
  const [maintenanceReady, setMaintenanceReady] = useState(false)
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatusPayload>({})
  const [systemMetrics, setSystemMetrics] = useState<SystemMetrics>({})
  const [drawerSovereign, setDrawerSovereign] = useState(false)
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

    // Send one initial status request; subsequent updates arrive via
    // real-time runtime_status_push events from the backend (no polling).
    const sent = sendCommand('app:get-runtime-status', {
      source: 'product_readiness_gate',
    })
    if (!sent.ok) {
      setMaintenanceReady(false)
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

    return () => {
      disposed = true
      window.removeEventListener('ipc_event', onStatusPush)
    }
  }, [connected, sendCommand])

  const connection = connectionCopy(backendSocket.status, maintenanceReady)
  const summary = useMemo(() => {
    const running = toolboxTools.filter((tool) => tool.status === 'running').length
    const issues = toolboxTools.filter(
      (tool) =>
        tool.status === 'error' ||
        (tool.launchable !== false && tool.runtimeAvailable === false)
    ).length
    return { running, issues, total: toolboxTools.length }
  }, [toolboxTools])
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

        {backendSocket.lastError && !connected && (
          <aside className="error-notice" role="alert" data-testid="backend-error">
            {t.backendConnectionFailed}
          </aside>
        )}

        {/* Clean tech-feel summary cards */}
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
        </section>

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
