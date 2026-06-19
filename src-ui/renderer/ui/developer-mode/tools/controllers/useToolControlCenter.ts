import {
  getBackendConnectionSnapshot,
  useBackendSocket,
} from '@/hooks/useBackendSocket'
import { useEffect, useRef, useState } from 'react'
import { eventBus } from '@/shared/RuntimeEventBus'
import {
  applyGlobalUpdatePlan,
  normalizeGlobalUpdatePlan,
  type GlobalUpdatePlan,
} from '@/shared/services/globalUpdateCoordinator'
import {
  INITIAL_STARTUP_MONITOR,
  resolveStartupMonitor,
  type StartupMonitorState,
} from '@/shared/core-system/core/startupMonitor'
import type { BusyAction, BusyActions, UrlConfigKey } from '../cards/types'
import { initialUrlDraft } from '../cards/types'

export interface ToolControlCenterState {
  busyActions: BusyActions
  autoToolsStarting: boolean
  urlDraft: Record<UrlConfigKey, string>
  settingsFeedback: string
  sandboxFeedback: string
  updateFeedback: string
  updateNonHotChangeCount: number
  updateNonHotChanges: string[]
  globalUpdatePlan: GlobalUpdatePlan | null
  backupFeedback: string
  logFeedback: string
  startupChecking: boolean
  startupMonitor: StartupMonitorState
  operationRecords: string[]
  sandboxIntervalMinutes: number
  updateIntervalMinutes: number
  backupIntervalMinutes: number
  logIntervalMinutes: number
  setSandboxIntervalMinutes: (minutes: number) => void
  setUpdateIntervalMinutes: (minutes: number) => void
  setBackupIntervalMinutes: (minutes: number) => void
  setLogIntervalMinutes: (minutes: number) => void
  handleUrlChange: (key: UrlConfigKey, value: string) => void
  saveUrlConfig: () => void
  increaseTextSize: () => void
  decreaseTextSize: () => void
  openProviderBrowser: (
    provider: 'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek'
  ) => void
  startAutoTools: () => void
  startSandboxTool: () => void
  stopSandboxTool: () => void
  startUpdateTool: () => void
  stopUpdateTool: () => void
  startBackupTool: () => void
  stopBackupTool: () => void
  startLogTool: () => void
  stopLogTool: () => void
  maintainSandbox: () => void
  checkSandboxHealth: () => void
  refreshUpdateStatus: () => void
  applyDetectedUpdates: () => void
  createBackupRecord: () => void
  deleteBackupRecord: () => void
  exportOperationLogs: () => void
  exportErrorLogs: () => void
  checkSystemStartup: () => void
  stopCurrentAction: () => void
}

const UI_ZOOM_STORAGE_KEY = 'gptbridge_ui_zoom_factor'
const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3
const UI_ZOOM_STEP = 0.05
const STARTUP_MONITOR_INTERVAL_MS = 5000
const STARTUP_CHECK_TIMEOUT_MS = 9000
const AUTO_TOOLS_STEP_DELAY_MS = 500
const TOOL_CHAIN_STEP_DELAY_MS = 400
const AUTO_TOOLS_DEFAULT_RETRY_DELAY_MS = 3000
const AUTO_INTERVAL_STORAGE_KEY = 'gptbridge_developer_tool_auto_intervals_v1'
const AUTO_TOOLS_DEFAULT_START_STORAGE_KEY =
  'gptbridge_developer_tool_runtime_default_started_v1'
const MIN_AUTO_INTERVAL_MINUTES = 1
const MAX_AUTO_INTERVAL_MINUTES = 24 * 60

type AutoToolKey = 'sandbox' | 'update' | 'backup' | 'logs'
type AutoToolIntervalMinutes = Record<AutoToolKey, number>

const DEFAULT_AUTO_TOOL_INTERVAL_MINUTES: AutoToolIntervalMinutes = {
  sandbox: 5,
  update: 3,
  backup: 20,
  logs: 10,
}

const AUTO_TOOL_BUSY_ACTION: Record<AutoToolKey, BusyAction> = {
  sandbox: 'sandbox-auto',
  update: 'update-auto',
  backup: 'backup-auto',
  logs: 'logs-auto',
}

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

function readSavedUiZoom(): number {
  try {
    const raw = window.localStorage.getItem(UI_ZOOM_STORAGE_KEY)
    const parsed = raw ? Number(raw) : 1
    if (Number.isNaN(parsed) || parsed <= 0) return 1
    return clampUiZoom(parsed)
  } catch {
    return 1
  }
}

function clampAutoIntervalMinutes(value: number): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return MIN_AUTO_INTERVAL_MINUTES
  const rounded = Math.round(parsed)
  return Math.max(MIN_AUTO_INTERVAL_MINUTES, Math.min(MAX_AUTO_INTERVAL_MINUTES, rounded))
}

function readSavedAutoToolIntervals(): AutoToolIntervalMinutes {
  try {
    const raw = window.localStorage.getItem(AUTO_INTERVAL_STORAGE_KEY)
    if (!raw) return DEFAULT_AUTO_TOOL_INTERVAL_MINUTES
    const parsed = JSON.parse(raw) as Partial<Record<AutoToolKey, unknown>>
    return {
      sandbox: clampAutoIntervalMinutes(Number(parsed.sandbox ?? DEFAULT_AUTO_TOOL_INTERVAL_MINUTES.sandbox)),
      update: clampAutoIntervalMinutes(Number(parsed.update ?? DEFAULT_AUTO_TOOL_INTERVAL_MINUTES.update)),
      backup: clampAutoIntervalMinutes(Number(parsed.backup ?? DEFAULT_AUTO_TOOL_INTERVAL_MINUTES.backup)),
      logs: clampAutoIntervalMinutes(Number(parsed.logs ?? DEFAULT_AUTO_TOOL_INTERVAL_MINUTES.logs)),
    }
  } catch {
    return DEFAULT_AUTO_TOOL_INTERVAL_MINUTES
  }
}

interface ToolControlCenterOptions {
  autoStartTools?: boolean
  startupMonitorEnabled?: boolean
}

export function useToolControlCenter({
  autoStartTools = false,
  startupMonitorEnabled = false,
}: ToolControlCenterOptions = {}): ToolControlCenterState {
  const { sendCommand, status: socketStatus } = useBackendSocket()
  const [busyActions, setBusyActions] = useState<BusyActions>([])
  const busyActionsRef = useRef<BusyActions>([])
  const autoRecoveringChatgptRef = useRef(false)
  const pendingChatgptHealthCheckRef = useRef(false)
  const autoRecoveringGeminiRef = useRef(false)
  const pendingGeminiHealthCheckRef = useRef(false)
  const pendingManualStartupCheckRef = useRef(false)
  const startupCheckInFlightRef = useRef(false)
  const startupCheckTimerRef = useRef<number | null>(null)
  const socketStatusRef = useRef(socketStatus)
  const autoToolsDefaultStartRef = useRef(false)
  const autoToolsDefaultRetryTimerRef = useRef<number | null>(null)
  const autoToolsTimersRef = useRef<number[]>([])
  const autoToolIntervalsRef = useRef<Partial<Record<AutoToolKey, number>>>({})
  const autoToolRunHandlersRef = useRef<Partial<Record<AutoToolKey, () => void>>>({})

  const [baseConfig, setBaseConfig] = useState<Record<string, unknown>>({})
  const [urlDraft, setUrlDraft] = useState<Record<UrlConfigKey, string>>(initialUrlDraft)
  const [autoToolIntervalMinutes, setAutoToolIntervalMinutes] =
    useState<AutoToolIntervalMinutes>(readSavedAutoToolIntervals)
  const autoToolIntervalMinutesRef = useRef<AutoToolIntervalMinutes>(autoToolIntervalMinutes)

  const [settingsFeedback, setSettingsFeedback] = useState('')
  const [sandboxFeedback, setSandboxFeedback] = useState('')
  const [updateFeedback, setUpdateFeedback] = useState('')
  const [updateNonHotChangeCount, setUpdateNonHotChangeCount] = useState(0)
  const [updateNonHotChanges, setUpdateNonHotChanges] = useState<string[]>([])
  const [globalUpdatePlan, setGlobalUpdatePlan] = useState<GlobalUpdatePlan | null>(null)
  const [backupFeedback, setBackupFeedback] = useState('')
  const [logFeedback, setLogFeedback] = useState('')
  const [startupChecking, setStartupChecking] = useState(false)
  const [startupMonitor, setStartupMonitor] = useState<StartupMonitorState>(INITIAL_STARTUP_MONITOR)
  const [operationRecords, setOperationRecords] = useState<string[]>([])

  const appendRecord = (message: string) => {
    const ts = new Date().toLocaleTimeString('zh-TW', { hour12: false })
    setOperationRecords((prev) => [`${ts} | ${message}`, ...prev].slice(0, 40))
  }

  const commitBusyActions = (next: BusyActions) => {
    busyActionsRef.current = next
    setBusyActions(next)
  }

  const addBusyAction = (action: BusyAction) => {
    const current = busyActionsRef.current
    if (current.includes(action)) return
    commitBusyActions([...current, action])
  }

  const removeBusyAction = (action: BusyAction) => {
    const current = busyActionsRef.current
    if (!current.includes(action)) return
    commitBusyActions(current.filter((item) => item !== action))
  }

  const removeBusyGroup = (actions: BusyAction[]) => {
    const keys = new Set(actions)
    const current = busyActionsRef.current
    const next = current.filter((item) => !keys.has(item))
    if (next.length === current.length) return
    commitBusyActions(next)
  }

  const hasBusyAction = (action: BusyAction): boolean =>
    busyActionsRef.current.includes(action)

  const isBackendSocketConnected = (): boolean =>
    socketStatusRef.current === 'Connected' ||
    getBackendConnectionSnapshot().connected

  const waitForSocketReady = (timeoutMs: number): Promise<boolean> => {
    if (isBackendSocketConnected()) return Promise.resolve(true)

    return new Promise((resolve) => {
      let settled = false
      let off: () => void = () => {}

      const finish = (ready: boolean) => {
        if (settled) return
        settled = true
        window.clearInterval(poll)
        window.clearTimeout(timer)
        off()
        resolve(ready)
      }

      const poll = window.setInterval(() => {
        if (isBackendSocketConnected()) finish(true)
      }, 150)

      const timer = window.setTimeout(() => {
        finish(isBackendSocketConnected())
      }, timeoutMs)

      off = eventBus.on<{ connected?: boolean }>(
        'socket_connected',
        (payload) => {
          if (!payload?.connected && !isBackendSocketConnected()) return
          finish(true)
        }
      )
    })
  }

  const ensureSocketReady = async (timeoutMs = 12000): Promise<boolean> => {
    if (isBackendSocketConnected()) return true

    const api = (window as any).electron
    if (api?.invoke) {
      try {
        await api.invoke('app:ensure-backend-started')
      } catch {
        // Ignore IPC restriction in minimal environments.
      }
    }

    if (isBackendSocketConnected()) return true
    return waitForSocketReady(timeoutMs)
  }

  const clearStartupTimer = () => {
    const timer = startupCheckTimerRef.current
    if (timer !== null) {
      window.clearTimeout(timer)
      startupCheckTimerRef.current = null
    }
  }

  const clearAutoToolsTimers = () => {
    for (const timer of autoToolsTimersRef.current) {
      window.clearTimeout(timer)
    }
    autoToolsTimersRef.current = []
  }

  const clearDefaultAutoToolsRetryTimer = () => {
    const timer = autoToolsDefaultRetryTimerRef.current
    if (timer === null) return
    window.clearTimeout(timer)
    autoToolsDefaultRetryTimerRef.current = null
  }

  const scheduleAutoToolsTask = (task: () => void, delayMs: number) => {
    const timer = window.setTimeout(() => {
      autoToolsTimersRef.current = autoToolsTimersRef.current.filter((id) => id !== timer)
      task()
    }, delayMs)
    autoToolsTimersRef.current.push(timer)
  }

  const isAutoToolRunning = (tool: AutoToolKey): boolean =>
    hasBusyAction(AUTO_TOOL_BUSY_ACTION[tool])

  const markAutoToolRunning = (tool: AutoToolKey, running: boolean) => {
    const key = AUTO_TOOL_BUSY_ACTION[tool]
    if (running) {
      addBusyAction(key)
    } else {
      removeBusyAction(key)
    }
  }

  const clearAutoToolInterval = (tool: AutoToolKey) => {
    const timer = autoToolIntervalsRef.current[tool]
    if (timer !== undefined) {
      window.clearInterval(timer)
      delete autoToolIntervalsRef.current[tool]
    }
  }

  const clearAllAutoToolIntervals = () => {
    const current = autoToolIntervalsRef.current
    for (const key of Object.keys(current) as AutoToolKey[]) {
      const timer = current[key]
      if (timer !== undefined) {
        window.clearInterval(timer)
      }
    }
    autoToolIntervalsRef.current = {}
    autoToolRunHandlersRef.current = {}
  }

  const getAutoToolIntervalMs = (tool: AutoToolKey): number => {
    const minutes = clampAutoIntervalMinutes(autoToolIntervalMinutesRef.current[tool])
    return minutes * 60 * 1000
  }

  const bindAutoToolInterval = (tool: AutoToolKey) => {
    const run = autoToolRunHandlersRef.current[tool]
    if (!run) return

    clearAutoToolInterval(tool)
    const timer = window.setInterval(() => {
      if (!isAutoToolRunning(tool)) return
      run()
    }, getAutoToolIntervalMs(tool))
    autoToolIntervalsRef.current[tool] = timer
  }

  const triggerStartupStatusCheck = async (
    source: 'manual' | 'auto'
  ): Promise<void> => {
    if (startupCheckInFlightRef.current) return

    startupCheckInFlightRef.current = true
    setStartupChecking(true)
    clearStartupTimer()

    const ready = await ensureSocketReady(source === 'manual' ? 12000 : 6000)
    if (!ready) {
      startupCheckInFlightRef.current = false
      setStartupChecking(false)
      if (source === 'manual') {
        pendingManualStartupCheckRef.current = false
        const message = '後端連線尚未就緒，啟動狀態檢查暫時無法執行。'
        setStartupMonitor((prev) => ({
          ...prev,
          level: 'warn',
          label: '待檢查',
          message,
          lastCheckedAt: Date.now(),
        }))
        appendRecord(message)
      } else {
        setStartupMonitor((prev) => ({
          ...prev,
          level: 'warn',
          label: '待檢查',
          message: '系統保活檢查中：後端尚未就緒，將持續自動重試。',
          lastCheckedAt: Date.now(),
        }))
      }
      return
    }

    sendCommand('mother_startup_status', {})

    startupCheckTimerRef.current = window.setTimeout(() => {
      startupCheckTimerRef.current = null
      if (!startupCheckInFlightRef.current) return
      startupCheckInFlightRef.current = false
      setStartupChecking(false)
      const timeoutMessage =
        source === 'manual'
          ? '系統啟動狀態檢查逾時，請稍後再試。'
          : '系統保活檢查逾時，將於下一輪自動重試。'
      if (source === 'manual') {
        pendingManualStartupCheckRef.current = false
        appendRecord(timeoutMessage)
      }
      setStartupMonitor((prev) => ({
        ...prev,
        level: 'warn',
        label: '待檢查',
        message: timeoutMessage,
        lastCheckedAt: Date.now(),
      }))
    }, STARTUP_CHECK_TIMEOUT_MS)
  }

  useEffect(() => {
    socketStatusRef.current = socketStatus
  }, [socketStatus])

  useEffect(() => {
    busyActionsRef.current = busyActions
  }, [busyActions])

  useEffect(() => {
    autoToolIntervalMinutesRef.current = autoToolIntervalMinutes
    try {
      window.localStorage.setItem(
        AUTO_INTERVAL_STORAGE_KEY,
        JSON.stringify(autoToolIntervalMinutes)
      )
    } catch {
      // Ignore localStorage restriction in locked environments.
    }
  }, [autoToolIntervalMinutes])

  useEffect(() => {
    return () => {
      clearDefaultAutoToolsRetryTimer()
      clearAutoToolsTimers()
      clearAllAutoToolIntervals()
    }
  }, [])

  const applyConfigDraft = (config: Record<string, unknown>) => {
    setBaseConfig(config)
    setUrlDraft({
      chatgpt_main_url: String(config.chatgpt_main_url ?? ''),
      gemini_main_url: String(config.gemini_main_url ?? ''),
      claude_main_url: String(config.claude_main_url ?? ''),
      perplexity_main_url: String(config.perplexity_main_url ?? ''),
      deepseek_main_url: String(config.deepseek_main_url ?? ''),
    })
  }

  useEffect(() => {
    addBusyAction('config-load')
    appendRecord('開始載入 URL 設定')
    sendCommand('load_config', {})
  }, [sendCommand])

  useEffect(() => {
    if (!startupMonitorEnabled) return
    let disposed = false

    const monitor = () => {
      if (disposed) return
      void triggerStartupStatusCheck('auto')
    }

    monitor()
    const timer = window.setInterval(monitor, STARTUP_MONITOR_INTERVAL_MS)

    return () => {
      disposed = true
      window.clearInterval(timer)
      clearStartupTimer()
      startupCheckInFlightRef.current = false
    }
  }, [sendCommand, socketStatus, startupMonitorEnabled])

  useEffect(() => {
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      const eventName = detail.event
      const payload = (detail.payload || {}) as Record<string, unknown>

      if (eventName === 'load_config_result') {
        removeBusyAction('config-load')
        if (!payload.ok) {
          const message = String(payload.message || '載入設定失敗')
          setSettingsFeedback(message)
          appendRecord(message)
          return
        }

        applyConfigDraft((payload.config as Record<string, unknown>) || {})
        setSettingsFeedback('URL 設定已載入')
        appendRecord('URL 設定載入完成')
        return
      }

      if (eventName === 'save_config_result') {
        removeBusyAction('config-save')
        const message = payload.ok
          ? String(payload.message || 'URL 已儲存')
          : String(payload.message || 'URL 儲存失敗')
        setSettingsFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'focus_chatgpt_result' || eventName === 'focus_gemini_result') {
        removeBusyGroup([
          'browser-chatgpt',
          'browser-gemini',
          'profile-reset-chatgpt',
          'profile-reset-gemini',
        ])

        if (!payload.ok) {
          const rawMessage = String(payload.message || '')
          const shouldAutoRecover =
            rawMessage.includes('BROWSER_LOCKED') ||
            rawMessage.includes('already in use') ||
            rawMessage.includes('Target page, context or browser has been closed')

          if (shouldAutoRecover) {
            const provider = eventName === 'focus_chatgpt_result' ? 'chatgpt' : 'gemini'
            const isChatgpt = provider === 'chatgpt'
            const recoveringRef = isChatgpt ? autoRecoveringChatgptRef : autoRecoveringGeminiRef

            if (!recoveringRef.current) {
              recoveringRef.current = true
              addBusyAction(isChatgpt ? 'profile-reset-chatgpt' : 'profile-reset-gemini')
              const message = isChatgpt
                ? '偵測到 ChatGPT Profile 被鎖定，正在重建共享 Profile。'
                : '偵測到 Gemini Profile 被鎖定，正在重建共享 Profile。'
              setSettingsFeedback(message)
              appendRecord(message)
              sendCommand('settings_reset_provider_profile', {
                provider,
                profile: 'main',
                launch_manual_auth: true,
                reason: isChatgpt
                  ? 'auto_recover_locked_profile'
                  : 'auto_recover_locked_profile_gemini',
              })
              return
            }
          }
        }

        const provider = eventName === 'focus_chatgpt_result' ? 'chatgpt' : 'gemini'
        const verificationRequired = Boolean(payload.verification_required)
        const message = payload.ok
          ? verificationRequired
            ? provider === 'gemini'
              ? 'Google 驗證仍在進行，請於系統 Edge 完成後再返回。'
              : 'Cloudflare 驗證仍在進行，請於系統 Edge 完成後再返回。'
            : 'AI 瀏覽器已聚焦'
          : String(payload.message || '聚焦 AI 瀏覽器失敗')
        setSettingsFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_open_system_browser_result') {
        const provider = String(payload.provider || 'chatgpt').toLowerCase()
        const busyActionByProvider: Record<string, BusyAction> = {
          chatgpt: 'browser-chatgpt',
          gemini: 'browser-gemini',
          claude: 'browser-claude',
          perplexity: 'browser-perplexity',
          deepseek: 'browser-deepseek',
        }
        const busyAction = busyActionByProvider[provider] || 'browser-chatgpt'
        removeBusyAction(busyAction)

        const successMessageByProvider: Record<string, string> = {
          chatgpt: '已開啟系統 Edge，請完成 ChatGPT 登入與驗證。',
          gemini: '已開啟系統 Edge，請完成 Google/Gemini 登入與驗證。',
          claude: '已開啟系統 Edge，請完成 Claude 登入與驗證。',
          perplexity: '已開啟系統 Edge，請完成 Perplexity 登入與驗證。',
          deepseek: '已開啟系統 Edge，請完成 DeepSeek 登入與驗證。',
        }
        const message = payload.ok
          ? successMessageByProvider[provider] || '已開啟系統 Edge。'
          : String(payload.message || '開啟系統瀏覽器失敗')
        setSettingsFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_reset_provider_profile_result') {
        const provider = String(payload.provider || 'chatgpt').toLowerCase()

        if (provider === 'gemini') {
          removeBusyAction('profile-reset-gemini')
        } else {
          removeBusyAction('profile-reset-chatgpt')
        }

        if (provider === 'chatgpt') {
          autoRecoveringChatgptRef.current = false
          pendingChatgptHealthCheckRef.current = false
        }
        if (provider === 'gemini') {
          autoRecoveringGeminiRef.current = false
          pendingGeminiHealthCheckRef.current = false
        }

        const message = payload.ok
          ? Boolean(payload.manual_auth_launched)
            ? provider === 'gemini'
              ? '已重建 Gemini 共享 Profile，請在系統 Edge 手動完成驗證。'
              : '已重建 ChatGPT 共享 Profile，請在系統 Edge 手動完成驗證。'
            : String(payload.message || `${provider} profile 重建完成`)
          : String(payload.message || `${provider} profile 重建失敗`)
        setSettingsFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'mother_provider_status_result') {
        const watchingChatgpt =
          pendingChatgptHealthCheckRef.current || autoRecoveringChatgptRef.current
        const watchingGemini =
          pendingGeminiHealthCheckRef.current || autoRecoveringGeminiRef.current

        if (!watchingChatgpt && !watchingGemini) return

        const chatgptStatus = String(payload.chatgpt_status || '').toUpperCase()
        const geminiStatus = String(payload.gemini_status || '').toUpperCase()

        if (chatgptStatus === 'UNAUTHENTICATED' && watchingChatgpt) {
          const message = 'ChatGPT 驗證尚未完成，請回到系統 Edge 完成驗證。'
          setSettingsFeedback(message)
          appendRecord(message)
          pendingChatgptHealthCheckRef.current = false
        } else if (watchingChatgpt) {
          pendingChatgptHealthCheckRef.current = false
        }

        if (geminiStatus === 'UNAUTHENTICATED' && watchingGemini) {
          const message = 'Google/Gemini 驗證尚未完成，請回到系統 Edge 完成驗證。'
          setSettingsFeedback(message)
          appendRecord(message)
          pendingGeminiHealthCheckRef.current = false
        } else if (watchingGemini) {
          pendingGeminiHealthCheckRef.current = false
        }
        return
      }

      if (eventName === 'mother_startup_status_result') {
        clearStartupTimer()
        startupCheckInFlightRef.current = false
        setStartupChecking(false)
        setStartupMonitor(resolveStartupMonitor(payload))

        if (pendingManualStartupCheckRef.current) {
          const message = payload.ok
            ? '系統啟動狀態檢查完成'
            : String(payload.message || '系統啟動狀態檢查失敗')
          appendRecord(message)
          pendingManualStartupCheckRef.current = false
        }
        return
      }

      if (eventName === 'settings_maintain_sandbox_result') {
        removeBusyAction('sandbox-maintain')
        const message = payload.ok
          ? String(payload.message || '沙箱維護完成')
          : String(payload.message || '沙箱維護失敗')
        setSandboxFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_health_refresh_result') {
        const source = String(payload.source || '').toLowerCase()

        if (source === 'update_tool') {
          removeBusyAction('update-refresh')
          const nonHotCount = Number(payload.non_hot_update_count ?? 0)
          const nonHotChanges = Array.isArray(payload.non_hot_update_changes)
            ? payload.non_hot_update_changes
                .filter((item): item is string => typeof item === 'string')
                .slice(0, 30)
            : []
          setUpdateNonHotChangeCount(Number.isFinite(nonHotCount) ? Math.max(0, nonHotCount) : 0)
          setUpdateNonHotChanges(nonHotChanges)
          setGlobalUpdatePlan(normalizeGlobalUpdatePlan(payload.global_update_plan))
          const message = payload.ok
            ? String(payload.message || '更新狀態檢查完成')
            : String(payload.message || '更新狀態檢查失敗')
          setUpdateFeedback(message)
          appendRecord(message)
          return
        }

        if (source === 'sandbox_tool') {
          removeBusyAction('sandbox-health')
        } else if (hasBusyAction('sandbox-health')) {
          removeBusyAction('sandbox-health')
        } else if (hasBusyAction('update-refresh')) {
          removeBusyAction('update-refresh')
        }

        const message = payload.ok
          ? String(payload.message || '沙箱健康檢查完成')
          : String(payload.message || '沙箱健康檢查失敗')
        setSandboxFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_mark_updates_applied_result') {
        removeBusyAction('update-apply')
        setGlobalUpdatePlan(normalizeGlobalUpdatePlan(payload.global_update_plan))
        const message = payload.ok
          ? String(payload.message || '全域更新基準已刷新')
          : String(payload.message || '全域更新基準刷新失敗')
        setUpdateFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_backup_records_result') {
        removeBusyAction('backup-record')
        const message = payload.ok
          ? String(payload.message || '備份記錄建立完成')
          : String(payload.message || '備份記錄建立失敗')
        setBackupFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_delete_backup_result') {
        removeBusyAction('backup-delete')
        const message = payload.ok
          ? String(payload.message || '備份刪除完成')
          : String(payload.message || '備份刪除失敗')
        setBackupFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_export_logs_result') {
        removeBusyAction('logs-export')
        const message = payload.ok
          ? String(payload.message || '操作紀錄匯出完成')
          : String(payload.message || '操作紀錄匯出失敗')
        setLogFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'settings_export_error_logs_result') {
        removeBusyAction('logs-export-errors')
        const message = payload.ok
          ? String(payload.message || '錯誤紀錄匯出完成')
          : String(payload.message || '錯誤紀錄匯出失敗')
        setLogFeedback(message)
        appendRecord(message)
        return
      }

      if (eventName === 'unhandled_command_result') {
        const message = String(payload.message || '指令未被後端處理')
        const pending = busyActionsRef.current
        const action = pending.length > 0 ? pending[pending.length - 1] : null
        commitBusyActions(
          pending.filter(
            (item) =>
              item === 'sandbox-auto' ||
              item === 'update-auto' ||
              item === 'backup-auto' ||
              item === 'logs-auto'
          )
        )

        if (action === 'sandbox-maintain' || action === 'sandbox-health') {
          setSandboxFeedback(message)
        } else if (action === 'update-refresh') {
          setUpdateFeedback(message)
        } else if (action === 'backup-record' || action === 'backup-delete') {
          setBackupFeedback(message)
        } else if (action === 'logs-export' || action === 'logs-export-errors') {
          setLogFeedback(message)
        } else {
          setSettingsFeedback(message)
        }

        appendRecord(message)
      }
    }

    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [sendCommand])

  const handleUrlChange = (key: UrlConfigKey, value: string) => {
    setUrlDraft((prev) => ({ ...prev, [key]: value }))
  }

  const saveUrlConfig = () => {
    const trimmed = {
      chatgpt_main_url: urlDraft.chatgpt_main_url.trim(),
      gemini_main_url: urlDraft.gemini_main_url.trim(),
      claude_main_url: urlDraft.claude_main_url.trim(),
      perplexity_main_url: urlDraft.perplexity_main_url.trim(),
      deepseek_main_url: urlDraft.deepseek_main_url.trim(),
    }

    const invalid = Object.values(trimmed).some(
      (value) => value.length === 0 || !/^https?:\/\//i.test(value)
    )

    if (invalid) {
      const message = 'URL 必須是 http:// 或 https:// 開頭'
      setSettingsFeedback(message)
      appendRecord(message)
      return
    }

    setSettingsFeedback('')
    addBusyAction('config-save')
    appendRecord('送出 URL 變更')
    sendCommand('save_config', { config: { ...baseConfig, ...trimmed } })
  }

  const setUiZoomFactor = async (busyKey: BusyAction, nextFactor: number) => {
    const api = (window as any).electron
    if (!api?.invoke) {
      const message = '目前環境不支援介面比例調整'
      setSettingsFeedback(message)
      appendRecord(message)
      return
    }

    addBusyAction(busyKey)
    setSettingsFeedback('')

    try {
      const result = (await api.invoke('app:set-ui-zoom', {
        factor: clampUiZoom(nextFactor),
      })) as { ok?: boolean; factor?: number; message?: string }

      if (!result?.ok) {
        const failedMessage = String(result?.message || '介面比例調整失敗')
        setSettingsFeedback(failedMessage)
        appendRecord(failedMessage)
        return
      }

      const factor = clampUiZoom(Number(result.factor ?? nextFactor))
      const percent = Math.round(factor * 100)

      try {
        window.localStorage.setItem(UI_ZOOM_STORAGE_KEY, String(factor))
      } catch {
        // Ignore storage failure in restricted environments.
      }

      const successMessage = `全域介面基準比例已調整為 ${percent}%`
      setSettingsFeedback(successMessage)
      appendRecord(successMessage)
    } catch (error) {
      const message =
        error instanceof Error ? error.message : '介面比例調整失敗（未知錯誤）'
      setSettingsFeedback(message)
      appendRecord(message)
    } finally {
      removeBusyAction(busyKey)
    }
  }

  const increaseTextSize = () => {
    const current = readSavedUiZoom()
    void setUiZoomFactor('font-increase', current + UI_ZOOM_STEP)
  }

  const decreaseTextSize = () => {
    const current = readSavedUiZoom()
    void setUiZoomFactor('font-decrease', current - UI_ZOOM_STEP)
  }

  const openProviderBrowser = (
    provider: 'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek'
  ) => {
    setSettingsFeedback('')
    const busyKeyByProvider: Record<
      'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek',
      BusyAction
    > = {
      chatgpt: 'browser-chatgpt',
      gemini: 'browser-gemini',
      claude: 'browser-claude',
      perplexity: 'browser-perplexity',
      deepseek: 'browser-deepseek',
    }
    const messageByProvider: Record<
      'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek',
      string
    > = {
      chatgpt: '已送出開啟系統 Edge（ChatGPT）',
      gemini: '已送出開啟系統 Edge（Google/Gemini）',
      claude: '已送出開啟系統 Edge（Claude）',
      perplexity: '已送出開啟系統 Edge（Perplexity）',
      deepseek: '已送出開啟系統 Edge（DeepSeek）',
    }
    const busyKey = busyKeyByProvider[provider]
    addBusyAction(busyKey)

    sendCommand('settings_open_system_browser', { provider })

    const message = messageByProvider[provider]
    setSettingsFeedback(message)
    appendRecord(message)

    window.setTimeout(() => {
      removeBusyAction(busyKey)
    }, 6000)
  }

  const checkSystemStartup = () => {
    pendingManualStartupCheckRef.current = true
    appendRecord('執行系統啟動狀態檢查')
    void triggerStartupStatusCheck('manual')
  }

  const triggerSandboxMaintenance = (skipConfirm = false): boolean => {
    if (hasBusyAction('sandbox-maintain')) return false

    if (!skipConfirm && !window.confirm('確定執行沙箱維護？')) {
      return false
    }
    setSandboxFeedback('')
    addBusyAction('sandbox-maintain')
    appendRecord('執行沙箱維護')
    sendCommand('settings_maintain_sandbox', {})
    return true
  }

  const checkSandboxHealth = () => {
    if (hasBusyAction('sandbox-health')) return

    setSandboxFeedback('')
    addBusyAction('sandbox-health')
    appendRecord('執行沙箱健康檢查')
    sendCommand('settings_health_refresh', { source: 'sandbox_tool' })
  }

  const refreshUpdateStatus = () => {
    if (hasBusyAction('update-refresh')) return

    setUpdateFeedback('')
    addBusyAction('update-refresh')
    appendRecord('執行熱更新檢查')
    sendCommand('settings_health_refresh', { source: 'update_tool' })
  }

  const applyDetectedUpdates = () => {
    if (hasBusyAction('update-apply')) return

    if (!globalUpdatePlan?.changed) {
      const message = '目前沒有待套用的全域更新。'
      setUpdateFeedback(message)
      appendRecord(message)
      return
    }

    addBusyAction('update-apply')
    appendRecord(`套用全域更新：${globalUpdatePlan.actionLabel}`)

    void applyGlobalUpdatePlan(globalUpdatePlan)
      .then((result) => {
        setUpdateFeedback(result.message)
        appendRecord(result.message)

        if (result.markApplied) {
          sendCommand('settings_mark_updates_applied', {
            strategy: result.strategy,
            source: 'global_update_coordinator',
          })
          return
        }

        removeBusyAction('update-apply')
      })
      .catch((error: unknown) => {
        removeBusyAction('update-apply')
        const message = error instanceof Error ? error.message : String(error)
        const feedback = `全域更新套用失敗：${message}`
        setUpdateFeedback(feedback)
        appendRecord(feedback)
      })
  }

  const createBackupRecord = () => {
    if (hasBusyAction('backup-record')) return

    setBackupFeedback('')
    addBusyAction('backup-record')
    appendRecord('建立備份記錄')
    sendCommand('settings_backup_records', {})
  }

  const deleteBackupRecord = () => {
    const rawTarget = window.prompt(
      '請輸入要刪除的備份檔名或完整路徑（例如：20260531_120000_settings-record.zip）',
      ''
    )
    if (rawTarget === null) return

    const target = rawTarget.trim()
    if (!target) {
      const message = '未輸入備份檔名，已取消刪除。'
      setBackupFeedback(message)
      appendRecord(message)
      return
    }

    const confirmed = window.confirm(`確定刪除備份：${target}？`)
    if (!confirmed) return

    setBackupFeedback('')
    addBusyAction('backup-delete')
    appendRecord(`刪除備份：${target}`)
    sendCommand('settings_delete_backup', { target })
  }

  const exportOperationLogs = () => {
    if (hasBusyAction('logs-export')) return

    setLogFeedback('')
    addBusyAction('logs-export')
    appendRecord('匯出操作紀錄')
    sendCommand('settings_export_logs', {})
  }

  const exportErrorLogs = () => {
    if (hasBusyAction('logs-export-errors')) return

    setLogFeedback('')
    addBusyAction('logs-export-errors')
    appendRecord('匯出錯誤紀錄')
    sendCommand('settings_export_error_logs', {})
  }

  const runSandboxCycle = () => {
    const started = triggerSandboxMaintenance(true)
    if (!started) return

    scheduleAutoToolsTask(() => {
      if (!isAutoToolRunning('sandbox')) return
      checkSandboxHealth()
    }, TOOL_CHAIN_STEP_DELAY_MS)
  }

  const runLogCycle = () => {
    exportOperationLogs()
    scheduleAutoToolsTask(() => {
      if (!isAutoToolRunning('logs')) return
      exportErrorLogs()
    }, TOOL_CHAIN_STEP_DELAY_MS)
  }

  const setAutoToolFeedback = (tool: AutoToolKey, message: string) => {
    if (tool === 'sandbox') {
      setSandboxFeedback(message)
      return
    }
    if (tool === 'update') {
      setUpdateFeedback(message)
      return
    }
    if (tool === 'backup') {
      setBackupFeedback(message)
      return
    }
    setLogFeedback(message)
  }

  const autoToolName = (tool: AutoToolKey): string => {
    if (tool === 'sandbox') return '沙箱工具'
    if (tool === 'update') return '更新工具'
    if (tool === 'backup') return '備份工具'
    return '日誌工具'
  }

  const applyAutoToolInterval = (tool: AutoToolKey, minutes: number) => {
    const normalized = clampAutoIntervalMinutes(minutes)

    setAutoToolIntervalMinutes((prev) => {
      const next = { ...prev, [tool]: normalized }
      autoToolIntervalMinutesRef.current = next
      return next
    })

    const message = `${autoToolName(tool)}自動週期已調整為 ${normalized} 分鐘`
    setAutoToolFeedback(tool, message)
    appendRecord(message)

    if (isAutoToolRunning(tool)) {
      bindAutoToolInterval(tool)
    }
  }

  const startAutoToolLoop = (
    tool: AutoToolKey,
    options: {
      onRun: () => void
      setFeedback: (message: string) => void
      notReadyMessage: string
      startRecord: string
    }
  ) => {
    if (isAutoToolRunning(tool)) return

    markAutoToolRunning(tool, true)
    clearAutoToolInterval(tool)
    options.setFeedback('')
    appendRecord(options.startRecord)

    void (async () => {
      const ready = await ensureSocketReady()
      if (!ready) {
        markAutoToolRunning(tool, false)
        clearAutoToolInterval(tool)
        options.setFeedback(options.notReadyMessage)
        appendRecord(options.notReadyMessage)
        return
      }

      if (!isAutoToolRunning(tool)) {
        return
      }

      autoToolRunHandlersRef.current[tool] = options.onRun
      options.onRun()
      bindAutoToolInterval(tool)
    })()
  }

  const stopAutoToolLoop = (
    tool: AutoToolKey,
    options: {
      clearBusy: BusyAction[]
      setFeedback: (message: string) => void
      stopMessage: string
    }
  ) => {
    clearAutoToolInterval(tool)
    delete autoToolRunHandlersRef.current[tool]
    markAutoToolRunning(tool, false)
    removeBusyGroup(options.clearBusy)
    options.setFeedback(options.stopMessage)
    appendRecord(options.stopMessage)
  }

  const startSandboxTool = () => {
    startAutoToolLoop('sandbox', {
      onRun: runSandboxCycle,
      setFeedback: setSandboxFeedback,
      notReadyMessage: '後端連線尚未就緒，沙箱工具無法啟動全自動執行。',
      startRecord: '沙箱工具已啟動全自動執行',
    })
  }

  const stopSandboxTool = () => {
    stopAutoToolLoop('sandbox', {
      clearBusy: ['sandbox-maintain', 'sandbox-health'],
      setFeedback: setSandboxFeedback,
      stopMessage: '沙箱工具已停止全自動執行',
    })
  }

  const startUpdateTool = () => {
    startAutoToolLoop('update', {
      onRun: refreshUpdateStatus,
      setFeedback: setUpdateFeedback,
      notReadyMessage: '後端連線尚未就緒，更新工具無法啟動全自動執行。',
      startRecord: '更新工具已啟動全自動執行',
    })
  }

  const stopUpdateTool = () => {
    stopAutoToolLoop('update', {
      clearBusy: ['update-refresh'],
      setFeedback: setUpdateFeedback,
      stopMessage: '更新工具已停止全自動執行',
    })
  }

  const startBackupTool = () => {
    startAutoToolLoop('backup', {
      onRun: createBackupRecord,
      setFeedback: setBackupFeedback,
      notReadyMessage: '後端連線尚未就緒，備份工具無法啟動全自動執行。',
      startRecord: '備份工具已啟動全自動執行',
    })
  }

  const stopBackupTool = () => {
    stopAutoToolLoop('backup', {
      clearBusy: ['backup-record', 'backup-delete'],
      setFeedback: setBackupFeedback,
      stopMessage: '備份工具已停止全自動執行',
    })
  }

  const startLogTool = () => {
    startAutoToolLoop('logs', {
      onRun: runLogCycle,
      setFeedback: setLogFeedback,
      notReadyMessage: '後端連線尚未就緒，日誌工具無法啟動全自動執行。',
      startRecord: '日誌工具已啟動全自動執行',
    })
  }

  const stopLogTool = () => {
    stopAutoToolLoop('logs', {
      clearBusy: ['logs-export', 'logs-export-errors'],
      setFeedback: setLogFeedback,
      stopMessage: '日誌工具已停止全自動執行',
    })
  }

  const maintainSandbox = () => {
    void (async () => {
      const ready = await ensureSocketReady()
      if (!ready) {
        const message = '後端連線尚未就緒，沙箱工具暫時無法執行。'
        setSandboxFeedback(message)
        appendRecord(message)
        return
      }
      runSandboxCycle()
    })()
  }

  const setSandboxIntervalMinutes = (minutes: number) => {
    applyAutoToolInterval('sandbox', minutes)
  }

  const setUpdateIntervalMinutes = (minutes: number) => {
    applyAutoToolInterval('update', minutes)
  }

  const setBackupIntervalMinutes = (minutes: number) => {
    applyAutoToolInterval('backup', minutes)
  }

  const setLogIntervalMinutes = (minutes: number) => {
    applyAutoToolInterval('logs', minutes)
  }

  const hasDefaultAutoToolsStarted = (): boolean => {
    if (autoToolsDefaultStartRef.current) return true

    try {
      if (window.sessionStorage.getItem(AUTO_TOOLS_DEFAULT_START_STORAGE_KEY) === '1') {
        autoToolsDefaultStartRef.current = true
        return true
      }
    } catch {
      // Ignore sessionStorage restriction in locked environments.
    }

    return false
  }

  const markDefaultAutoToolsStarted = () => {
    autoToolsDefaultStartRef.current = true

    try {
      window.sessionStorage.setItem(AUTO_TOOLS_DEFAULT_START_STORAGE_KEY, '1')
    } catch {
      // Ignore sessionStorage restriction in locked environments.
    }
  }

  const runAutoTools = async ({
    markDefaultStart = false,
    retryOnNotReady = false,
  }: {
    markDefaultStart?: boolean
    retryOnNotReady?: boolean
  } = {}): Promise<boolean> => {
    if (hasBusyAction('tools-auto-start')) return false

    setSettingsFeedback('')
    setSandboxFeedback('')
    setUpdateFeedback('')
    setBackupFeedback('')
    setLogFeedback('')
    addBusyAction('tools-auto-start')
    appendRecord('已啟動工具執行層自動流程')
    clearAutoToolsTimers()

    const ready = await ensureSocketReady()
    if (!ready) {
      removeBusyAction('tools-auto-start')
      const message = retryOnNotReady
        ? '後端連線尚未就緒，自動流程將稍後重試。'
        : '後端連線尚未就緒，自動流程已取消。'
      setSettingsFeedback(message)
      appendRecord(message)
      return false
    }

    if (markDefaultStart) {
      markDefaultAutoToolsStarted()
    }

    const tasks: Array<() => void> = [
      startSandboxTool,
      startUpdateTool,
      startBackupTool,
      startLogTool,
    ]

    tasks.forEach((task, index) => {
      scheduleAutoToolsTask(task, index * AUTO_TOOLS_STEP_DELAY_MS)
    })

    scheduleAutoToolsTask(() => {
      removeBusyAction('tools-auto-start')
      appendRecord('工具執行層已切換為全自動持續執行')
    }, tasks.length * AUTO_TOOLS_STEP_DELAY_MS + 100)

    return true
  }

  const startAutoTools = () => {
    void runAutoTools({ markDefaultStart: true })
  }

  useEffect(() => {
    if (!autoStartTools || hasDefaultAutoToolsStarted()) return

    let disposed = false

    const scheduleDefaultStartAttempt = (delayMs: number) => {
      clearDefaultAutoToolsRetryTimer()
      autoToolsDefaultRetryTimerRef.current = window.setTimeout(() => {
        autoToolsDefaultRetryTimerRef.current = null

        if (disposed || hasDefaultAutoToolsStarted()) return

        void runAutoTools({
          markDefaultStart: true,
          retryOnNotReady: true,
        }).then((started) => {
          if (disposed || started || hasDefaultAutoToolsStarted()) return
          scheduleDefaultStartAttempt(AUTO_TOOLS_DEFAULT_RETRY_DELAY_MS)
        })
      }, delayMs)
    }

    scheduleDefaultStartAttempt(0)

    return () => {
      disposed = true
      clearDefaultAutoToolsRetryTimer()
    }
  }, [autoStartTools])

  const stopCurrentAction = () => {
    const current = busyActionsRef.current
    commitBusyActions([])
    setStartupChecking(false)
    clearStartupTimer()
    clearAutoToolsTimers()
    clearAllAutoToolIntervals()
    startupCheckInFlightRef.current = false
    pendingManualStartupCheckRef.current = false
    autoRecoveringChatgptRef.current = false
    autoRecoveringGeminiRef.current = false
    pendingChatgptHealthCheckRef.current = false
    pendingGeminiHealthCheckRef.current = false

    const message = current.length > 0
      ? `已停止目前操作：${current.join(', ')}`
      : '目前沒有進行中的操作'
    appendRecord(message)
  }

  const autoToolsStarting = busyActions.includes('tools-auto-start')

  return {
    busyActions,
    autoToolsStarting,
    urlDraft,
    settingsFeedback,
    sandboxFeedback,
    updateFeedback,
    updateNonHotChangeCount,
    updateNonHotChanges,
    globalUpdatePlan,
    backupFeedback,
    logFeedback,
    startupChecking,
    startupMonitor,
    operationRecords,
    sandboxIntervalMinutes: autoToolIntervalMinutes.sandbox,
    updateIntervalMinutes: autoToolIntervalMinutes.update,
    backupIntervalMinutes: autoToolIntervalMinutes.backup,
    logIntervalMinutes: autoToolIntervalMinutes.logs,
    setSandboxIntervalMinutes,
    setUpdateIntervalMinutes,
    setBackupIntervalMinutes,
    setLogIntervalMinutes,
    handleUrlChange,
    saveUrlConfig,
    increaseTextSize,
    decreaseTextSize,
    openProviderBrowser,
    startAutoTools,
    startSandboxTool,
    stopSandboxTool,
    startUpdateTool,
    stopUpdateTool,
    startBackupTool,
    stopBackupTool,
    startLogTool,
    stopLogTool,
    maintainSandbox,
    checkSandboxHealth,
    refreshUpdateStatus,
    applyDetectedUpdates,
    createBackupRecord,
    deleteBackupRecord,
    exportOperationLogs,
    exportErrorLogs,
    checkSystemStartup,
    stopCurrentAction,
  }
}

