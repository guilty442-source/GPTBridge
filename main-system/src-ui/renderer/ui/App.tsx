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
    const statusTimer = window.setInterval(() => void refreshStatus(), 5000)

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
      window.clearInterval(statusTimer)
    }
  }, [])

  useEffect(() => {
    if (serviceManager.getAllStates().length === 0) {
      void startStartupPipeline()
    }
  }, [])

  useEffect(() => {
    let disposed = false
    let retryTimer: number | null = null

    if (!connected) {
      setMaintenanceReady(false)
      return () => undefined
    }

    const pollMaintenance = async () => {
      try {
        const result = waitForIpcEvent('app:get-runtime-status_result', 10000)
        const sent = sendCommand('app:get-runtime-status', {
          source: 'product_readiness_gate',
        })
        if (!sent.ok) throw new Error(sent.message || t.maintenanceUnavailable)
        const payload = await result
        if (disposed) return
        const ready = payload.maintenance_ready === true
        setMaintenanceReady(ready)
        setRuntimeStatus(payload as RuntimeStatusPayload)
        if (ready) return
      } catch {
        if (disposed) return
        setMaintenanceReady(false)
      }
      retryTimer = window.setTimeout(() => void pollMaintenance(), 1000)
    }

    void pollMaintenance()
    return () => {
      disposed = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
    }
  }, [connected, sendCommand, waitForIpcEvent])

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

        <section className="status-overview" aria-label={t.systemOverview}>
          <article><span>{t.availableTools}</span><strong>{summary.total}</strong><small>{t.availableToolsHint}</small></article>
          <article><span>{t.runningTools}</span><strong>{summary.running}</strong><small>{t.runningToolsHint}</small></article>
          <article><span>{t.issues}</span><strong>{summary.issues}</strong><small>{t.issuesHint}</small></article>
          <article><span>{t.commandStrategy}</span><strong className="status-overview__word">{t.requestToolExecution}</strong><small>{t.commandStrategyHint}</small></article>
          <article data-testid="system-disk-size">
            <span>{t.systemDisk} {systemMetrics.diskRoot || ''}</span>
            <strong className="status-overview__word">
              {formatBytes(systemMetrics.diskTotalBytes, { exactBytes: true, fallback: t.pendingCheck })}
            </strong>
            <small>
              {t.usageRate}{' '}
              {typeof systemMetrics.diskUsagePercent === 'number'
                ? `${systemMetrics.diskUsagePercent.toFixed(1)}%`
                : t.pendingCheck}{' · '}
              {t.used} {formatBytes(diskUsedBytes, { exactBytes: true, fallback: t.pendingCheck })} · {t.available}{' '}
              {formatBytes(systemMetrics.diskFreeBytes, { exactBytes: true, fallback: t.pendingCheck })}
            </small>
          </article>
          <article data-testid="main-system-folder-size">
            <span>{t.mainSystemSize}</span>
            <strong className="status-overview__word">
              {formatProjectSize(mainSystemSizeBytes, { fallback: t.pendingCheck })}
            </strong>
            <small>{t.mainSystemSizeHint} · {capacityDetail(mainSystemSizeBytes, mainSystemFileCount)}</small>
          </article>
          <article data-testid="dependency-folder-size">
            <span>{t.dependencySize}</span>
            <strong className="status-overview__word">
              {formatProjectSize(dependencySizeBytes, { fallback: t.pendingCheck })}
            </strong>
            <small>{t.dependencySizeHint} · {capacityDetail(dependencySizeBytes, dependencyFileCount)}</small>
          </article>
          <article data-testid="shared-layer-folder-size">
            <span>{t.sharedLayerSize}</span>
            <strong className="status-overview__word">
              {formatProjectSize(sharedLayerSizeBytes, { fallback: t.pendingCheck })}
            </strong>
            <small>{t.sharedLayerSizeHint} · {capacityDetail(sharedLayerSizeBytes, sharedLayerFileCount)}</small>
          </article>
          <article data-testid="workspace-folder-size">
            <span>{t.workspaceSize}</span>
            <strong className="status-overview__word">
              {formatProjectSize(workspaceSizeBytes, { fallback: t.pendingCheck })}
            </strong>
            <small>{t.workspaceSizeHint} · {capacityDetail(workspaceSizeBytes, workspaceFileCount)}</small>
          </article>
        </section>

        <SovereignDashboard runtimeStatus={runtimeStatus} />

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
    </div>
  )
}
