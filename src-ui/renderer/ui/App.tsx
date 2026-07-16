import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  serviceManager,
  startStartupPipeline,
} from '@/services/RuntimeServiceManager'
import { useBackendSocket } from '@/hooks/useBackendSocket'
import { ToolboxEntry } from '@/ui/toolbox/ToolboxEntry'
import { useToolboxApplications } from '@/ui/toolbox/useToolboxApplications'
import {
  applyGlobalUpdatePlan,
  normalizeGlobalUpdatePlan,
} from '@/shared/services/globalUpdateCoordinator'
import { formatBytes } from '@/shared/utils/format'
import '../App.css'

const UI_ZOOM_STORAGE_KEY = 'gptbridge_ui_zoom_factor'
const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3

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
      label: '後端已連線',
      detail: '安全連線與啟動維護均已完成',
      tone: 'online',
    }
  }
  if (status === 'Connected') {
    return {
      label: '啟動維護中',
      detail: '正在完成版本相容與自動修正',
      tone: 'pending',
    }
  }
  if (status === 'Connecting' || status === 'Repairing') {
    return {
      label: status === 'Repairing' ? '自動修復中' : '正在連線',
      detail: '正在檢查並恢復後端服務',
      tone: 'pending',
    }
  }
  return {
    label: '後端未連線',
    detail: '操作已鎖定，系統會自動重新連線',
    tone: 'offline',
  }
}

export default function App() {
  const [appVersion, setAppVersion] = useState('1.0.0')
  const [updateMessage, setUpdateMessage] = useState('尚未檢查更新')
  const [checkingUpdate, setCheckingUpdate] = useState(false)
  const [maintenanceReady, setMaintenanceReady] = useState(false)
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
          reject(new Error(`等待後端回應逾時：${eventName}`))
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
        if (!sent.ok) throw new Error(sent.message || '無法取得啟動維護狀態。')
        const payload = await result
        if (disposed) return
        const ready = payload.maintenance_ready === true
        setMaintenanceReady(ready)
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

  const handleUpdate = useCallback(async () => {
    if (!operational || checkingUpdate) return
    setCheckingUpdate(true)
    setUpdateMessage('正在檢查更新…')

    try {
      const waitPlan = waitForIpcEvent('settings_health_refresh_result', 20000)
      const sent = sendCommand('settings_health_refresh', {
        source: 'product_update_control',
      })
      if (!sent.ok) throw new Error(sent.message || '更新檢查未送出。')
      const payload = await waitPlan
      if (payload.ok === false) {
        throw new Error(String(payload.message || '更新檢查失敗。'))
      }

      const plan = normalizeGlobalUpdatePlan(payload.global_update_plan)
      if (!plan.changed) {
        setUpdateMessage('目前已是最新狀態')
        return
      }

      const applied = await applyGlobalUpdatePlan(plan)
      if (!applied.ok) throw new Error(applied.message)

      if (applied.markApplied) {
        const waitMark = waitForIpcEvent(
          'settings_mark_updates_applied_result',
          20000
        )
        const markSent = sendCommand('settings_mark_updates_applied', {
          strategy: applied.strategy,
        })
        if (!markSent.ok) {
          throw new Error(markSent.message || '更新套用狀態未送出。')
        }
        await waitMark
      }
      setUpdateMessage(applied.message)
    } catch (error) {
      setUpdateMessage(error instanceof Error ? error.message : String(error))
    } finally {
      setCheckingUpdate(false)
    }
  }, [checkingUpdate, operational, sendCommand, waitForIpcEvent])

  const connection = connectionCopy(backendSocket.status, maintenanceReady)
  const summary = useMemo(() => {
    const running = toolboxTools.filter((tool) => tool.status === 'running').length
    const issues = toolboxTools.filter((tool) => tool.status === 'error').length
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
            <span>應用程式控制中心</span>
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
        <section className="hero-panel">
          <div className="hero-copy">
            <span className="eyebrow">WORKSPACE CONTROL</span>
            <h1>一個入口，管理所有獨立應用程式</h1>
            <p>
              主程式只負責啟動、停止與熱更新。每個工具保有獨立程式碼、資料庫、權限與自動修正程序，降低升級互相影響的風險。
            </p>
          </div>

          <div className="hero-actions">
            <button
              type="button"
              className="button button--primary button--update"
              data-testid="check-updates"
              disabled={!operational || checkingUpdate}
              onClick={() => void handleUpdate()}
            >
              {checkingUpdate ? '檢查中…' : '檢查並套用更新'}
            </button>
            <span>{updateMessage}</span>
          </div>
        </section>

        {!operational && (
          <aside className="connection-notice" role="status">
            <strong>{connected ? '啟動維護尚未完成' : '離線安全模式'}</strong>
            <span>
              {connected
                ? '正在檢查版本相容性並執行各工具的自動修正，完成前不開放狀態變更。'
                : '後端連線中斷時，狀態變更指令不會送出或排隊；系統會自動修復並重新連線。'}
            </span>
          </aside>
        )}

        {backendSocket.lastError && !connected && (
          <aside className="error-notice" role="alert" data-testid="backend-error">
            {backendSocket.lastError}
          </aside>
        )}

        <section className="status-overview" aria-label="系統概況">
          <article><span>可用工具</span><strong>{summary.total}</strong><small>已註冊的獨立應用程式</small></article>
          <article><span>執行中</span><strong>{summary.running}</strong><small>目前由主程式管理</small></article>
          <article><span>需要處理</span><strong>{summary.issues}</strong><small>偵測到異常的工具</small></article>
          <article><span>指令策略</span><strong className="status-overview__word">不排隊</strong><small>斷線時立即拒絕</small></article>
          <article data-testid="system-disk-size">
            <span>主系統硬碟 {systemMetrics.diskRoot || ''}</span>
            <strong className="status-overview__word">
              {formatBytes(systemMetrics.diskTotalBytes, { fallback: '待檢查' })}
            </strong>
            <small>
              已用 {formatBytes(diskUsedBytes, { fallback: '待檢查' })} · 可用{' '}
              {formatBytes(systemMetrics.diskFreeBytes, { fallback: '待檢查' })}
            </small>
          </article>
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
        <span>Windows 11 · Android companion · Local-first</span>
      </footer>
    </div>
  )
}
