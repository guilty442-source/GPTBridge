import { CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { RuntimeService } from '@/shared/types/runtime'
import type { ServiceState } from '@/services/RuntimeServiceManager'
import {
  serviceManager,
  startStartupPipeline,
} from '@/services/RuntimeServiceManager'
import { eventBus } from '@/shared/RuntimeEventBus'
import {
  INITIAL_STARTUP_MONITOR,
  resolveStartupMonitor,
  type StartupMonitorState,
} from '@/shared/core-system/core/startupMonitor'
import { useBackendSocket } from '@/hooks/useBackendSocket'
import { zhTW } from '@/i18n/zhTW'
import { DeveloperMode } from '@/ui/DeveloperMode'
import {
  createInitialToolRuntimeState as createInitialDeveloperToolRuntimeState,
  resolveToolAction as resolveDeveloperToolAction,
} from '@/ui/developer-mode/tools/runtimeState'
import { useToolControlCenter } from '@/ui/developer-mode/tools/controllers/useToolControlCenter'
import type {
  ToolAction,
  ToolRuntimeState,
} from '@/ui/developer-mode/tools/types'
import { GovernanceRulesPage } from '@/ui/governance-rules'
import { ToolboxEntry } from '@/ui/toolbox/ToolboxEntry'
import { useToolboxApplications } from '@/ui/toolbox/useToolboxApplications'

type ViewMode = 'info' | 'toolbox' | 'governance' | 'developer'
type SystemHealth = 'READY' | 'STARTING' | 'ERROR'
type LightLevel = 'green' | 'yellow' | 'red' | 'gray'

interface SystemMetrics {
  cpuUsagePercent: number | null
  ramUsagePercent: number | null
  ramTotalBytes: number | null
  ramFreeBytes: number | null
  diskUsagePercent: number | null
  diskTotalBytes: number | null
  diskFreeBytes: number | null
  sampledAt: number | null
}

interface AppStatusPayload {
  systemMetrics?: Partial<SystemMetrics>
}

const EMPTY_METRICS: SystemMetrics = {
  cpuUsagePercent: null,
  ramUsagePercent: null,
  ramTotalBytes: null,
  ramFreeBytes: null,
  diskUsagePercent: null,
  diskTotalBytes: null,
  diskFreeBytes: null,
  sampledAt: null,
}

const UI_ZOOM_STORAGE_KEY = 'gptbridge_ui_zoom_factor'
const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3
const STARTUP_MONITOR_INTERVAL_MS = 5000
const STARTUP_CHECK_TIMEOUT_MS = 9000

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

function toRuntimeService(service: ServiceState): RuntimeService {
  return {
    id: service.id,
    name: service.name,
    status: service.status,
    elapsed: service.elapsed,
    error: service.error,
  }
}

function lampColor(level: LightLevel): string {
  if (level === 'green') return '#34d399'
  if (level === 'yellow') return '#fbbf24'
  if (level === 'red') return '#f87171'
  return '#64748b'
}

function lampLabel(level: LightLevel): string {
  if (level === 'green') return '運作中'
  if (level === 'yellow') return '警示'
  if (level === 'red') return '異常'
  return '未啟動'
}

function formatPercent(value: number | null): string {
  if (value === null || Number.isNaN(value)) return '--'
  return `${Math.round(value)}%`
}

function formatGB(value: number | null): string {
  if (value === null || Number.isNaN(value)) return '--'
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function usageLevel(
  value: number | null,
  warningThreshold: number,
  abnormalThreshold: number
): LightLevel {
  if (value === null) return 'gray'
  if (value >= abnormalThreshold) return 'red'
  if (value >= warningThreshold) return 'yellow'
  return 'green'
}

function startupLevelToLightLevel(level: StartupMonitorState['level']): LightLevel {
  if (level === 'ok') return 'green'
  if (level === 'error') return 'red'
  if (level === 'warn') return 'yellow'
  return 'gray'
}

function formatCheckedAt(timestamp: number | null): string {
  if (!timestamp) return '尚未檢查'
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

export default function App() {
  const [activeView, setActiveView] = useState<ViewMode>('toolbox')
  const [services, setServices] = useState<RuntimeService[]>(
    serviceManager.getAllStates().map(toRuntimeService)
  )
  const [isModeMenuOpen, setIsModeMenuOpen] = useState(false)
  const [developerTools, setDeveloperTools] = useState<ToolRuntimeState[]>(() =>
    createInitialDeveloperToolRuntimeState()
  )
  const [metrics, setMetrics] = useState<SystemMetrics>(EMPTY_METRICS)
  const [startupChecking, setStartupChecking] = useState(false)
  const [startupMonitor, setStartupMonitor] = useState<StartupMonitorState>(
    INITIAL_STARTUP_MONITOR
  )
  const backendSocket = useBackendSocket()
  const sendCommand = backendSocket.sendCommand
  const startupCheckInFlightRef = useRef(false)
  const startupCheckTimerRef = useRef<number | null>(null)
  const developerToolControl = useToolControlCenter({ autoStartTools: true })

  const waitForIpcEvent = useCallback(
    (eventName: string, timeoutMs: number): Promise<Record<string, unknown>> => {
      return new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => {
          window.removeEventListener('ipc_event', handler)
          reject(new Error(`等待事件逾時：${eventName}`))
        }, timeoutMs)

        const handler = (event: Event) => {
          const customEvent = event as CustomEvent
          const detail = customEvent.detail || {}
          if (detail.event !== eventName) return
          window.clearTimeout(timer)
          window.removeEventListener('ipc_event', handler)
          resolve((detail.payload || {}) as Record<string, unknown>)
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
    handleToolboxAction,
  } = useToolboxApplications({
    activeView,
    backendStatus: backendSocket.status,
    sendCommand,
    waitForIpcEvent,
  })

  const clearStartupCheckTimer = useCallback(() => {
    if (startupCheckTimerRef.current === null) return
    window.clearTimeout(startupCheckTimerRef.current)
    startupCheckTimerRef.current = null
  }, [])

  const triggerStartupStatusCheck = useCallback(
    (source: 'manual' | 'auto' = 'auto') => {
      if (startupCheckInFlightRef.current) return

      if (backendSocket.status !== 'Connected') {
        setStartupChecking(false)
        setStartupMonitor((prev) => ({
          ...prev,
          level: 'warn',
          label: '待檢查',
          message:
            source === 'manual'
              ? '後端連線尚未就緒，系統檢查暫時無法執行。'
              : '檢查系統預設啟動中：後端尚未連線，將自動重試。',
          lastCheckedAt: Date.now(),
        }))
        return
      }

      startupCheckInFlightRef.current = true
      setStartupChecking(true)
      clearStartupCheckTimer()

      const result = sendCommand('mother_startup_status', {
        source: 'main_status_lights',
        trigger: source,
      })
      if (!result.ok && !result.queued) {
        startupCheckInFlightRef.current = false
        setStartupChecking(false)
        setStartupMonitor((prev) => ({
          ...prev,
          level: 'warn',
          label: '待檢查',
          message: '系統檢查命令送出失敗，將於下一輪自動重試。',
          lastCheckedAt: Date.now(),
        }))
        return
      }

      startupCheckTimerRef.current = window.setTimeout(() => {
        startupCheckTimerRef.current = null
        if (!startupCheckInFlightRef.current) return
        startupCheckInFlightRef.current = false
        setStartupChecking(false)
        setStartupMonitor((prev) => ({
          ...prev,
          level: 'warn',
          label: '待檢查',
          message:
            source === 'manual'
              ? '系統啟動狀態檢查逾時，請稍後再試。'
              : '系統燈號自動檢查逾時，將於下一輪自動重試。',
          lastCheckedAt: Date.now(),
        }))
      }, STARTUP_CHECK_TIMEOUT_MS)
    },
    [backendSocket.status, clearStartupCheckTimer, sendCommand]
  )

  useEffect(() => {
    const api = (window as any).electron
    if (!api?.invoke) return

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
  }, [])

  useEffect(() => {
    const off = eventBus.on('service_update', (nextServices: ServiceState[]) => {
      setServices(nextServices.map(toRuntimeService))
    })

    if (serviceManager.getAllStates().length === 0) {
      void startStartupPipeline()
    }

    return () => {
      off()
    }
  }, [])

  useEffect(() => {
    let disposed = false

    const refreshStatus = async () => {
      const api = (window as any).electron
      if (!api?.invoke) return

      try {
        const payload = (await api.invoke('app:get-status')) as AppStatusPayload
        const systemMetrics = payload.systemMetrics
        if (disposed || !systemMetrics) return

        setMetrics((prev) => ({
          ...prev,
          ...systemMetrics,
          sampledAt: systemMetrics.sampledAt ?? Date.now(),
        }))
      } catch {
        // Keep previous metrics on transient failure.
      }
    }

    void refreshStatus()
    const timer = window.setInterval(() => {
      void refreshStatus()
    }, 5000)

    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== 'mother_startup_status_result') return
      clearStartupCheckTimer()
      startupCheckInFlightRef.current = false
      setStartupChecking(false)
      setStartupMonitor(
        resolveStartupMonitor((detail.payload || {}) as Record<string, unknown>)
      )
    }

    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [clearStartupCheckTimer])

  useEffect(() => {
    let disposed = false

    const monitor = () => {
      if (disposed) return
      triggerStartupStatusCheck('auto')
    }

    monitor()
    const timer = window.setInterval(monitor, STARTUP_MONITOR_INTERVAL_MS)

    return () => {
      disposed = true
      window.clearInterval(timer)
      clearStartupCheckTimer()
      startupCheckInFlightRef.current = false
    }
  }, [clearStartupCheckTimer, triggerStartupStatusCheck])

  const systemStatus = useMemo<SystemHealth>(() => {
    if (services.length === 0) return 'STARTING'
    if (services.some((s) => s.status === 'FAIL' || s.status === 'TIMEOUT')) {
      return 'ERROR'
    }
    if (
      services.some(
        (s) =>
          s.status === 'INIT' ||
          s.status === 'BOOTING' ||
          s.status === 'DEGRADED'
      )
    ) {
      return 'STARTING'
    }
    return 'READY'
  }, [services])

  const statusColor = useMemo(() => {
    if (systemStatus === 'ERROR') return '#f87171'
    if (systemStatus === 'STARTING') return '#fbbf24'
    return '#34d399'
  }, [systemStatus])

  const serviceSummary = useMemo(() => {
    let normal = 0
    let abnormal = 0
    let notStarted = 0

    for (const service of services) {
      if (service.status === 'SUCCESS') {
        normal += 1
      } else if (service.status === 'FAIL' || service.status === 'TIMEOUT') {
        abnormal += 1
      } else {
        notStarted += 1
      }
    }

    return { normal, abnormal, notStarted }
  }, [services])

  const toolboxSummary = useMemo(() => {
    let running = 0
    let stopped = 0
    let abnormal = 0

    for (const tool of toolboxTools) {
      if (tool.status === 'error') {
        abnormal += 1
      } else if (tool.status === 'running' || tool.status === 'starting') {
        running += 1
      } else {
        stopped += 1
      }
    }

    return { running, stopped, abnormal }
  }, [toolboxTools])

  const lightSystem = useMemo<
    Array<{ key: string; title: string; detail: string; level: LightLevel }>
  >(() => {
    const backend = services.find(
      (service) =>
        service.id === 'backend' ||
        service.name.toLowerCase().includes('backend')
    )

    const browserLevel: LightLevel =
      backendSocket.status === 'Connected'
        ? 'green'
        : backendSocket.status === 'Error'
          ? 'red'
          : 'gray'

    const aiLevel: LightLevel =
      backendSocket.status === 'Connected'
        ? 'green'
        : backendSocket.status === 'Error'
          ? 'red'
          : 'gray'

    const toolboxLevel: LightLevel =
      toolboxSummary.abnormal > 0
        ? 'red'
        : toolboxSummary.running > 0
          ? 'green'
          : 'gray'

    return [
      {
        key: 'overall',
        title: '系統整體',
        detail:
          systemStatus === 'READY'
            ? '核心服務穩定運作中'
            : systemStatus === 'STARTING'
              ? '部分服務尚在檢查中'
              : '偵測到系統異常，請盡快處理',
        level:
          systemStatus === 'READY'
            ? 'green'
            : systemStatus === 'STARTING'
              ? 'yellow'
              : 'red',
      },
      {
        key: 'backend',
        title: '後端服務',
        detail: backend ? `${backend.name}: ${backend.status}` : '後端尚未啟動',
        level: !backend
          ? 'gray'
          : backend.status === 'SUCCESS'
            ? 'green'
            : backend.status === 'FAIL' || backend.status === 'TIMEOUT'
              ? 'red'
              : 'yellow',
      },
      {
        key: 'browser',
        title: '瀏覽器',
        detail:
          browserLevel === 'green'
            ? '瀏覽器通道已連線'
            : browserLevel === 'red'
              ? '瀏覽器通道異常'
              : '瀏覽器未啟動',
        level: browserLevel,
      },
      {
        key: 'ai',
        title: 'AI 連線',
        detail:
          aiLevel === 'green'
            ? 'AI 通道已連線'
            : aiLevel === 'red'
              ? 'AI 通道異常'
              : 'AI 通道未啟動',
        level: aiLevel,
      },
      {
        key: 'cpu',
        title: 'CPU',
        detail:
          metrics.cpuUsagePercent === null
            ? '尚未取樣（未啟動）'
            : `使用率 ${formatPercent(metrics.cpuUsagePercent)}`,
        level: usageLevel(metrics.cpuUsagePercent, 70, 90),
      },
      {
        key: 'ram',
        title: 'RAM',
        detail:
          metrics.ramUsagePercent === null
            ? '尚未取樣（未啟動）'
            : `使用率 ${formatPercent(metrics.ramUsagePercent)}，可用 ${formatGB(
                metrics.ramFreeBytes
              )}`,
        level: usageLevel(metrics.ramUsagePercent, 75, 92),
      },
      {
        key: 'disk',
        title: '系統空間',
        detail:
          metrics.diskUsagePercent === null
            ? '尚未取樣（未啟動）'
            : `使用率 ${formatPercent(metrics.diskUsagePercent)}，可用 ${formatGB(
                metrics.diskFreeBytes
              )}`,
        level: usageLevel(metrics.diskUsagePercent, 80, 95),
      },
      {
        key: 'toolbox',
        title: zhTW.toolbox.title,
        detail:
          toolboxSummary.abnormal > 0
            ? `異常 ${toolboxSummary.abnormal} 項`
            : toolboxSummary.running > 0
              ? `運作中 ${toolboxSummary.running} 項`
              : `未啟動 ${toolboxSummary.stopped} 項`,
        level: toolboxLevel,
      },
      {
        key: 'startup',
        title: startupChecking ? '檢查系統' : '啟動流程',
        detail: startupChecking
          ? '預設啟動檢查執行中...'
          : `${startupMonitor.label}｜後端 ${startupMonitor.backend}｜瀏覽器 ${startupMonitor.browserContext}｜${startupMonitor.message}`,
        level: startupChecking
          ? 'yellow'
          : startupLevelToLightLevel(startupMonitor.level),
      },
    ]
  }, [
    backendSocket.status,
    metrics.cpuUsagePercent,
    metrics.diskFreeBytes,
    metrics.diskUsagePercent,
    metrics.ramFreeBytes,
    metrics.ramUsagePercent,
    services,
    startupChecking,
    startupMonitor.backend,
    startupMonitor.browserContext,
    startupMonitor.label,
    startupMonitor.level,
    startupMonitor.message,
    systemStatus,
    toolboxSummary.abnormal,
    toolboxSummary.running,
    toolboxSummary.stopped,
  ])

  const handleDeveloperToolAction = (toolId: string, action: ToolAction) => {
    setDeveloperTools((prev) =>
      resolveDeveloperToolAction(prev, toolId, action, 'pending')
    )
    window.setTimeout(() => {
      setDeveloperTools((prev) =>
        resolveDeveloperToolAction(prev, toolId, action, 'settled')
      )
    }, 320)
  }

  return (
    <div style={styles.app}>
      <div style={styles.brandName}>{zhTW.app.platformName}</div>

      <main style={styles.contentArea}>
        <div style={styles.viewWrapper}>
          <div style={styles.viewTopBar}>
            <div style={styles.relativeWrapper}>
              {isModeMenuOpen && (
                <div style={styles.modeDropdown}>
                  <button
                    type="button"
                    style={activeView === 'info' ? styles.modeItemActive : styles.modeItem}
                    onClick={() => {
                      setActiveView('info')
                      setIsModeMenuOpen(false)
                    }}
                  >
                    {zhTW.app.backHome}
                  </button>
                  <button
                    type="button"
                    style={
                      activeView === 'toolbox' ? styles.modeItemActive : styles.modeItem
                    }
                    onClick={() => {
                      setActiveView('toolbox')
                      setIsModeMenuOpen(false)
                    }}
                  >
                    {zhTW.toolbox.title}
                  </button>
                  <button
                    type="button"
                    style={
                      activeView === 'developer' ? styles.modeItemActive : styles.modeItem
                    }
                    onClick={() => {
                      setActiveView('developer')
                      setIsModeMenuOpen(false)
                    }}
                  >
                    {zhTW.developer.title}
                  </button>
                  <button
                    type="button"
                    style={
                      activeView === 'governance' ? styles.modeItemActive : styles.modeItem
                    }
                    onClick={() => {
                      setActiveView('governance')
                      setIsModeMenuOpen(false)
                    }}
                  >
                    {zhTW.governanceRulesPage.title}
                  </button>
                </div>
              )}

              <button
                type="button"
                style={styles.modeTrigger}
                onClick={() => setIsModeMenuOpen((prev) => !prev)}
              >
                {`模式切換/主畫面: ${
                  activeView === 'info'
                    ? zhTW.mode.interfaceSystem
                    : activeView === 'developer'
                      ? zhTW.developer.title
                      : activeView === 'governance'
                        ? zhTW.governanceRulesPage.title
                        : zhTW.toolbox.title
                }`}
              </button>
            </div>
          </div>

          {activeView === 'info' && (
            <section style={styles.toolCard}>
              <div style={styles.toolHeader}>
                <span style={styles.toolEmoji}>資訊</span>
                <div>
                  <div style={styles.toolName}>{zhTW.mode.interfaceSystem}</div>
                  <div style={styles.toolDesc}>
                    檢查系統已整合到主畫面燈號，開啟後會預設自動檢查。
                  </div>
                </div>
              </div>

              <div style={styles.lightSystemPanel}>
                <div style={styles.lightSystemHeaderRow}>
                  <div>
                    <div style={styles.lightSystemHeader}>系統燈號</div>
                    <div style={styles.lightSystemSubheader}>
                      {startupChecking
                        ? '預設啟動檢查執行中...'
                        : `最近檢查：${formatCheckedAt(startupMonitor.lastCheckedAt)}`}
                    </div>
                  </div>
                  <button
                    type="button"
                    style={styles.lightCheckButton}
                    onClick={() => triggerStartupStatusCheck('manual')}
                    disabled={startupChecking}
                  >
                    {startupChecking ? '檢查中...' : '立即檢查'}
                  </button>
                </div>
                <div style={styles.lightSystemGrid}>
                  {lightSystem.map((light) => (
                    <div key={light.key} style={styles.lightItem}>
                      <div style={styles.lightRow}>
                        <div
                          style={{
                            ...styles.lightDot,
                            backgroundColor: lampColor(light.level),
                            boxShadow: `0 0 12px ${lampColor(light.level)}`,
                          }}
                        />
                        <div style={styles.lightTitle}>{light.title}</div>
                        <div
                          style={{
                            ...styles.lightState,
                            color: lampColor(light.level),
                            borderColor: lampColor(light.level),
                          }}
                        >
                          {lampLabel(light.level)}
                        </div>
                      </div>
                      <div style={styles.lightDesc}>{light.detail}</div>
                    </div>
                  ))}
                </div>
              </div>

              <div style={styles.infoGrid}>
                <div style={styles.infoCard}>
                  <span style={styles.infoLabel}>檢查系統</span>
                  <strong
                    style={{
                      ...styles.infoValue,
                      color: lampColor(startupLevelToLightLevel(startupMonitor.level)),
                    }}
                  >
                    {startupChecking ? '檢查中' : startupMonitor.label}
                  </strong>
                </div>
                <div style={styles.infoCard}>
                  <span style={styles.infoLabel}>異常</span>
                  <strong style={{ ...styles.infoValue, color: '#f87171' }}>
                    {serviceSummary.abnormal}
                  </strong>
                </div>
                <div style={styles.infoCard}>
                  <span style={styles.infoLabel}>未啟動</span>
                  <strong style={{ ...styles.infoValue, color: '#64748b' }}>
                    {serviceSummary.notStarted}
                  </strong>
                </div>
              </div>
            </section>
          )}

          {activeView === 'toolbox' && (
            <ToolboxEntry
              tools={toolboxTools}
              syncing={toolboxSyncing}
              syncedAt={toolboxSyncedAt}
              onToolAction={handleToolboxAction}
            />
          )}

          {activeView === 'developer' && (
            <DeveloperMode
              services={services}
              systemStatus={systemStatus}
              tools={developerTools}
              toolControl={developerToolControl}
              onToolAction={handleDeveloperToolAction}
            />
          )}

          {activeView === 'governance' && (
            <GovernanceRulesPage
              sendCommand={sendCommand}
              socketStatus={backendSocket.status}
              onBack={() => setActiveView('toolbox')}
            />
          )}
        </div>
      </main>
    </div>
  )
}

const styles: Record<string, CSSProperties> = {
  app: {
    width: '100vw',
    height: '100vh',
    background: '#0b0f17',
    color: '#f5f7fb',
    fontFamily: '"Noto Sans TC", "Segoe UI", sans-serif',
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'stretch',
  },
  statusCenter: {
    marginTop: '44px',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    textAlign: 'center',
  },
  brandName: {
    position: 'fixed',
    top: '12px',
    left: '14px',
    zIndex: 60,
    fontSize: '13px',
    fontWeight: 900,
    color: '#94a3b8',
    letterSpacing: '1px',
    lineHeight: 1.1,
    marginBottom: 0,
    padding: '4px 8px',
    borderRadius: '8px',
    border: '1px solid #243044',
    background: 'rgba(11, 15, 23, 0.85)',
  },
  statusLightContainer: {
    background: 'transparent',
    border: 'none',
    cursor: 'pointer',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: '7px',
    padding: '10px 14px',
  },
  largeStatusDot: {
    width: '11px',
    height: '11px',
    borderRadius: '50%',
  },
  statusText: {
    fontSize: '18px',
    fontWeight: 900,
    letterSpacing: '-1px',
  },
  detailGrid: {
    marginTop: '12px',
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    padding: '16px 24px',
    background: '#111827',
    borderRadius: '16px',
    border: '1px solid #243044',
    minWidth: '280px',
  },
  detailItemRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
  },
  miniStatusDot: {
    width: '8px',
    height: '8px',
    borderRadius: '50%',
  },
  detailItem: {
    fontSize: '12px',
    color: '#94a3b8',
    fontWeight: 600,
  },
  contentArea: {
    flex: 1,
    width: '100%',
    display: 'flex',
    justifyContent: 'center',
    paddingTop: '6px',
    overflowY: 'auto',
  },
  viewWrapper: {
    width: '100%',
    maxWidth: '100%',
    padding: '0 10px 12px',
  },
  viewTopBar: {
    display: 'flex',
    justifyContent: 'flex-start',
    alignItems: 'center',
    marginBottom: '8px',
    minHeight: '34px',
    position: 'relative',
    zIndex: 80,
  },
  toolCard: {
    background: '#111827',
    border: '1px solid #243044',
    borderRadius: '14px',
    padding: '16px',
    boxShadow: '0 20px 50px rgba(0,0,0,0.5)',
  },
  toolHeader: {
    display: 'flex',
    gap: '12px',
    alignItems: 'center',
    marginBottom: '14px',
  },
  toolEmoji: {
    fontSize: '14px',
    fontWeight: 700,
    color: '#7dd3fc',
  },
  toolName: {
    fontSize: '14px',
    fontWeight: 900,
    color: '#f8fafc',
  },
  toolDesc: {
    fontSize: '14px',
    color: '#94a3b8',
  },
  lightSystemPanel: {
    marginBottom: '12px',
    background: '#0b1220',
    border: '1px solid #1e293b',
    borderRadius: '10px',
    padding: '10px',
  },
  lightSystemHeader: {
    color: '#cbd5e1',
    fontSize: '12px',
    fontWeight: 700,
    letterSpacing: '0.08em',
  },
  lightSystemHeaderRow: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: '12px',
    marginBottom: '8px',
  },
  lightSystemSubheader: {
    marginTop: '3px',
    color: '#64748b',
    fontSize: '11px',
    fontWeight: 700,
  },
  lightCheckButton: {
    border: '1px solid #38bdf8',
    borderRadius: '999px',
    padding: '5px 10px',
    color: '#082f49',
    background: '#7dd3fc',
    fontSize: '11px',
    fontWeight: 900,
    cursor: 'pointer',
  },
  lightSystemGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
    gap: '8px',
  },
  lightItem: {
    background: '#0f172a',
    border: '1px solid #1e293b',
    borderRadius: '8px',
    padding: '8px',
  },
  lightRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    marginBottom: '4px',
  },
  lightDot: {
    width: '7px',
    height: '7px',
    borderRadius: '50%',
    flexShrink: 0,
  },
  lightTitle: {
    color: '#e2e8f0',
    fontSize: '13px',
    fontWeight: 700,
    flex: 1,
  },
  lightState: {
    fontSize: '11px',
    fontWeight: 700,
    border: '1px solid',
    borderRadius: '999px',
    padding: '2px 7px',
  },
  lightDesc: {
    color: '#94a3b8',
    fontSize: '12px',
    lineHeight: 1.4,
  },
  infoGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
    gap: '8px',
  },
  infoCard: {
    background: '#0f172a',
    border: '1px solid #1e293b',
    borderRadius: '10px',
    padding: '10px',
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
  },
  infoLabel: {
    color: '#94a3b8',
    fontSize: '13px',
    fontWeight: 600,
  },
  infoValue: {
    color: '#f8fafc',
    fontSize: '24px',
    fontWeight: 800,
  },
  relativeWrapper: {
    position: 'relative',
    zIndex: 85,
  },
  modeTrigger: {
    padding: '7px 12px',
    background: '#111827',
    border: '1px solid #243044',
    borderRadius: '10px',
    color: '#f8fafc',
    fontSize: '11px',
    fontWeight: 600,
    cursor: 'pointer',
  },
  modeDropdown: {
    position: 'absolute',
    top: '38px',
    left: 0,
    minWidth: '140px',
    background: '#111827',
    border: '1px solid #243044',
    borderRadius: '12px',
    padding: '8px',
    boxShadow: '0 20px 40px rgba(0,0,0,0.5)',
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
    zIndex: 90,
  },
  modeItem: {
    width: '100%',
    padding: '8px 12px',
    borderRadius: '8px',
    color: '#94a3b8',
    fontSize: '12px',
    textAlign: 'left',
    cursor: 'pointer',
    border: 'none',
    background: 'transparent',
  },
  modeItemActive: {
    width: '100%',
    padding: '8px 12px',
    borderRadius: '8px',
    color: '#f8fafc',
    background: '#243044',
    fontSize: '12px',
    fontWeight: 700,
    textAlign: 'left',
    cursor: 'pointer',
    border: 'none',
  },
}

