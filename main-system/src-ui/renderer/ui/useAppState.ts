import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  serviceManager,
  startStartupPipeline,
} from '@/services/RuntimeServiceManager'
import { useBackendSocket } from '@/hooks/useBackendSocket'
import type { PendingActionApproval, RuntimeStatusPayload } from '@/ui/sovereign/SovereignDashboard'
import { mainSystemLocale } from '@/locales/main-system'

const UI_ZOOM_STORAGE_KEY = 'gptbridge_ui_zoom_factor'
const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3
const t = mainSystemLocale.product
const xr = mainSystemLocale.xingchengReport

export type SystemMetrics = {
  diskUsagePercent?: number | null
  diskTotalBytes?: number | null
  diskFreeBytes?: number | null
  diskRoot?: string
}

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

export function useAppState() {
  const [appVersion, setAppVersion] = useState('1.0.0')
  const [maintenanceReady, setMaintenanceReady] = useState(false)
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatusPayload>({})
  const [systemMetrics, setSystemMetrics] = useState<SystemMetrics>({})
  const [confirmBusyId, setConfirmBusyId] = useState<string | null>(null)
  const [switchBusy, setSwitchBusy] = useState<string | null>(null)
  const [confirmMessages, setConfirmMessages] = useState<Record<string, string>>({})
  const pendingActionsRef = useRef<PendingActionApproval[]>([])
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

  const confirmPendingAction = useCallback(
    async (actionId: string) => {
      if (!actionId || confirmBusyId) return
      const action = pendingActionsRef.current.find(
        (item) => item.action_id === actionId
      )
      const kind = String(action?.kind || '').trim()
      if (kind !== 'repair' && kind !== 'update') {
        setConfirmMessages((prev) => ({
          ...prev,
          [actionId]: xr.confirmedFailed,
        }))
        return
      }
      const confirmCommand = `xingcheng-confirm-automatic-${kind}`
      const executeCommand = `sync-execute-approved-automatic-${kind}`
      setConfirmBusyId(actionId)
      try {
        const confirmSent = sendCommand(confirmCommand, { action_id: actionId })
        if (!confirmSent.ok) {
          setConfirmMessages((prev) => ({
            ...prev,
            [actionId]: confirmSent.message || xr.confirmQueueFailed,
          }))
          return
        }
        const confirmResult = await waitForIpcEvent(
          `${confirmCommand}_result`,
          12000,
          (payload) =>
            !payload.action_id || String(payload.action_id) === actionId
        )
        if (confirmResult.ok !== true) {
          setConfirmMessages((prev) => ({
            ...prev,
            [actionId]:
              String(confirmResult.message || '').trim() || xr.confirmedFailed,
          }))
          return
        }
        const confirmationId = String(confirmResult.confirmation_id || '')
        const executeSent = sendCommand(executeCommand, {
          action_id: actionId,
          confirmation_id: confirmationId,
        })
        if (!executeSent.ok) {
          setConfirmMessages((prev) => ({
            ...prev,
            [actionId]: executeSent.message || xr.confirmQueueFailed,
          }))
          return
        }
        const executeResult = await waitForIpcEvent(
          `${executeCommand}_result`,
          120000,
          (payload) =>
            !payload.action_id || String(payload.action_id) === actionId
        )
        const ok = executeResult.ok === true
        setConfirmMessages((prev) => ({
          ...prev,
          [actionId]: ok
            ? xr.confirmedDone
            : String(executeResult.message || '').trim() || xr.confirmedFailed,
        }))
      } catch {
        setConfirmMessages((prev) => ({
          ...prev,
          [actionId]: xr.confirmedFailed,
        }))
      } finally {
        setConfirmBusyId(null)
        sendCommand('app:get-runtime-status', {
          source: 'pending_action_confirmation',
        })
      }
    },
    [confirmBusyId, sendCommand, waitForIpcEvent]
  )

  const setAutomationSwitch = useCallback(
    async (switchName: string, enabled: boolean) => {
      if (switchBusy) return
      const command =
        switchName === 'automatic_repair_enabled'
          ? 'xingcheng-set-repair-release'
          : 'xingcheng-set-update-release'
      setSwitchBusy(switchName)
      try {
        const sent = sendCommand(command, { enabled })
        if (!sent.ok) {
          setConfirmMessages((prev) => ({
            ...prev,
            [switchName]: sent.message || xr.switchSetFailed,
          }))
          return
        }
        const result = await waitForIpcEvent(`${command}_result`, 10000)
        if (result.ok !== true) {
          setConfirmMessages((prev) => ({
            ...prev,
            [switchName]: String(result.message || '') || xr.switchSetFailed,
          }))
        } else {
          setConfirmMessages((prev) => {
            const next = { ...prev }
            delete next[switchName]
            return next
          })
        }
      } catch {
        setConfirmMessages((prev) => ({
          ...prev,
          [switchName]: xr.switchSetFailed,
        }))
      } finally {
        setSwitchBusy(null)
        sendCommand('app:get-runtime-status', {
          source: 'automation_switch_update',
        })
      }
    },
    [switchBusy, sendCommand, waitForIpcEvent]
  )

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

    const onStatusPush = (event: Event) => {
      if (disposed) return
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
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

  const pendingActions = Array.isArray(runtimeStatus.pending_actions)
    ? runtimeStatus.pending_actions
    : []
  pendingActionsRef.current = pendingActions
  const automationSwitches = runtimeStatus.automation_switches || {}
  const repairSwitchOn = automationSwitches.automatic_repair_enabled === true
  const updateSwitchOn = automationSwitches.automatic_update_enabled === true
  const cardinality = runtimeStatus.pending_action_cardinality || {}

  return {
    appVersion,
    maintenanceReady,
    runtimeStatus,
    systemMetrics,
    confirmBusyId,
    switchBusy,
    confirmMessages,
    backendSocket,
    sendCommand,
    connected,
    operational,
    waitForIpcEvent,
    confirmPendingAction,
    setAutomationSwitch,
    pendingActions,
    repairSwitchOn,
    updateSwitchOn,
    cardinality,
  }
}
