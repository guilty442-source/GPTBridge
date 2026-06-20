import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { RuntimeService } from '@/shared/types/runtime'
import {
  INITIAL_STARTUP_MONITOR,
  resolveStartupMonitor,
  type StartupMonitorState,
} from '@/shared/core-system/core/startupMonitor'

export type SystemHealth = 'READY' | 'STARTING' | 'ERROR'
export type LightLevel = 'green' | 'yellow' | 'red' | 'gray'

type SendCommandResult = {
  ok: boolean
  queued: boolean
  message?: string
}

type SendCommand = (
  command: string,
  payload?: unknown
) => SendCommandResult

type ToolboxSummary = {
  running: number
  stopped: number
  abnormal: number
}

type UseSystemMonitorOptions = {
  services: RuntimeService[]
  backendStatus: string
  sendCommand: SendCommand
  toolboxTitle: string
  toolboxSummary: ToolboxSummary
}

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

const STARTUP_MONITOR_INTERVAL_MS = 5000
const STARTUP_CHECK_TIMEOUT_MS = 9000

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

export function lampColor(level: LightLevel): string {
  if (level === 'green') return '#34d399'
  if (level === 'yellow') return '#fbbf24'
  if (level === 'red') return '#f87171'
  return '#64748b'
}

export function lampLabel(level: LightLevel): string {
  if (level === 'green') return '運作中'
  if (level === 'yellow') return '警示'
  if (level === 'red') return '異常'
  return '未啟動'
}

export function startupLevelToLightLevel(
  level: StartupMonitorState['level']
): LightLevel {
  if (level === 'ok') return 'green'
  if (level === 'error') return 'red'
  if (level === 'warn') return 'yellow'
  return 'gray'
}

export function formatCheckedAt(timestamp: number | null): string {
  if (!timestamp) return '尚未檢查'
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

export function useSystemMonitor({
  services,
  backendStatus,
  sendCommand,
  toolboxTitle,
  toolboxSummary,
}: UseSystemMonitorOptions) {
  const [metrics, setMetrics] = useState<SystemMetrics>(EMPTY_METRICS)
  const [startupChecking, setStartupChecking] = useState(false)
  const [startupMonitor, setStartupMonitor] = useState<StartupMonitorState>(
    INITIAL_STARTUP_MONITOR
  )
  const startupCheckInFlightRef = useRef(false)
  const startupCheckTimerRef = useRef<number | null>(null)

  const clearStartupCheckTimer = useCallback(() => {
    if (startupCheckTimerRef.current === null) return
    window.clearTimeout(startupCheckTimerRef.current)
    startupCheckTimerRef.current = null
  }, [])

  const triggerStartupStatusCheck = useCallback(
    (source: 'manual' | 'auto' = 'auto') => {
      if (startupCheckInFlightRef.current) return

      if (backendStatus !== 'Connected') {
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
    [backendStatus, clearStartupCheckTimer, sendCommand]
  )

  useEffect(() => {
    let disposed = false

    const refreshStatus = async () => {
      const api = window.electron
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

  const lightSystem = useMemo<
    Array<{ key: string; title: string; detail: string; level: LightLevel }>
  >(() => {
    const backend = services.find(
      (service) =>
        service.id === 'backend' ||
        service.name.toLowerCase().includes('backend')
    )

    const browserLevel: LightLevel =
      backendStatus === 'Connected'
        ? 'green'
        : backendStatus === 'Error'
          ? 'red'
          : 'gray'

    const aiLevel: LightLevel =
      backendStatus === 'Connected'
        ? 'green'
        : backendStatus === 'Error'
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
        title: toolboxTitle,
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
    backendStatus,
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
    toolboxTitle,
  ])

  return {
    lightSystem,
    serviceSummary,
    startupChecking,
    startupMonitor,
    systemStatus,
    triggerStartupStatusCheck,
  }
}
