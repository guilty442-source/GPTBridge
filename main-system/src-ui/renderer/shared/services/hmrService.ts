type RecoveryMode = 'healthy' | 'warning' | 'recovering' | 'force-restart'

export type HMRInspectorSnapshot = {
  level: number
  mode: RecoveryMode
  lastReason: string
  lastEventAt: number | null
  lastSuccessAt: number | null
  failureCount: number
  disconnectCount: number
  recoveryPendingSince: number | null
}

type EscalationOptions = {
  minLevel?: number
  forceRestart?: boolean
}

const STATE_KEY = 'gptbridge_hmr_inspector_state'
const MAX_LEVEL = 5
const ACTION_COOLDOWN_MS = 2500
const STABLE_RESET_MS = 10000
const DISCONNECT_GRACE_MS = 3000
const RECOVERY_TIMEOUT_MS = 12000

const defaultSnapshot: HMRInspectorSnapshot = {
  level: 0,
  mode: 'healthy',
  lastReason: '',
  lastEventAt: null,
  lastSuccessAt: null,
  failureCount: 0,
  disconnectCount: 0,
  recoveryPendingSince: null,
}

const listeners = new Set<(snapshot: HMRInspectorSnapshot) => void>()

let snapshot = readPersistedSnapshot()
let initialized = false
let stableResetTimer: ReturnType<typeof setTimeout> | null = null
let watchdogTimer: ReturnType<typeof setInterval> | null = null
let disconnectTimer: ReturnType<typeof setTimeout> | null = null
let lastActionAt = 0
let fullReloadMarks: number[] = []

function readPersistedSnapshot(): HMRInspectorSnapshot {
  try {
    const raw = sessionStorage.getItem(STATE_KEY)
    if (!raw) return { ...defaultSnapshot }
    const parsed = JSON.parse(raw) as Partial<HMRInspectorSnapshot>
    return {
      level:
        typeof parsed.level === 'number' && Number.isFinite(parsed.level)
          ? Math.max(0, Math.min(MAX_LEVEL, Math.trunc(parsed.level)))
          : 0,
      mode:
        parsed.mode === 'healthy' ||
        parsed.mode === 'warning' ||
        parsed.mode === 'recovering' ||
        parsed.mode === 'force-restart'
          ? parsed.mode
          : 'healthy',
      lastReason: typeof parsed.lastReason === 'string' ? parsed.lastReason : '',
      lastEventAt:
        typeof parsed.lastEventAt === 'number' && Number.isFinite(parsed.lastEventAt)
          ? parsed.lastEventAt
          : null,
      lastSuccessAt:
        typeof parsed.lastSuccessAt === 'number' && Number.isFinite(parsed.lastSuccessAt)
          ? parsed.lastSuccessAt
          : null,
      failureCount:
        typeof parsed.failureCount === 'number' && Number.isFinite(parsed.failureCount)
          ? Math.max(0, Math.trunc(parsed.failureCount))
          : 0,
      disconnectCount:
        typeof parsed.disconnectCount === 'number' && Number.isFinite(parsed.disconnectCount)
          ? Math.max(0, Math.trunc(parsed.disconnectCount))
          : 0,
      recoveryPendingSince:
        typeof parsed.recoveryPendingSince === 'number' &&
        Number.isFinite(parsed.recoveryPendingSince)
          ? parsed.recoveryPendingSince
          : null,
    }
  } catch {
    return { ...defaultSnapshot }
  }
}

function persistSnapshot(): void {
  sessionStorage.setItem(STATE_KEY, JSON.stringify(snapshot))
}

function emit(): void {
  persistSnapshot()
  const current = { ...snapshot }
  for (const listener of listeners) {
    listener(current)
  }
}

function updateSnapshot(patch: Partial<HMRInspectorSnapshot>): void {
  snapshot = { ...snapshot, ...patch }
  emit()
}

function scheduleStableReset(): void {
  if (stableResetTimer) clearTimeout(stableResetTimer)
  stableResetTimer = setTimeout(() => {
    snapshot = {
      ...snapshot,
      level: 0,
      mode: 'healthy',
      lastReason: '',
      recoveryPendingSince: null,
    }
    emit()
  }, STABLE_RESET_MS)
}

function markHmrHealthy(reason: string): void {
  const now = Date.now()
  updateSnapshot({
    mode: 'healthy',
    lastReason: reason,
    lastEventAt: now,
    lastSuccessAt: now,
    recoveryPendingSince: null,
  })

  if (snapshot.level > 0) {
    scheduleStableReset()
  }
}

function isMajorFailure(reason: string): boolean {
  const text = reason.toLowerCase()
  return (
    text.includes('beforefullreload') ||
    text.includes('failed to fetch dynamically imported module') ||
    text.includes('loading chunk') ||
    text.includes('cannot apply hmr') ||
    text.includes('full reload')
  )
}

async function invokeElectron(channel: string): Promise<boolean> {
  const api = (window as any).electron
  if (!api?.invoke) return false

  try {
    await api.invoke(channel)
    return true
  } catch (error) {
    console.error(`[HMR Inspector] ipc ${channel} failed:`, error)
    return false
  }
}

async function performRecovery(level: number, reason: string): Promise<void> {
  const now = Date.now()
  if (now - lastActionAt < ACTION_COOLDOWN_MS) return
  lastActionAt = now

  if (level <= 1) return

  if (level === 2) {
    window.location.reload()
    return
  }

  if (level === 3) {
    const ok = await invokeElectron('app:reload-window')
    if (!ok) window.location.reload()
    return
  }

  if (level === 4) {
    const ok = await invokeElectron('app:reload-window-hard')
    if (!ok) window.location.reload()
    return
  }

  const restarted = await invokeElectron('app:restart')
  if (!restarted) {
    console.error('[HMR Inspector] app restart failed, fallback to page reload')
    window.location.reload()
  }

  console.error(`[HMR Inspector] forced app restart due to: ${reason}`)
}

async function escalate(reason: string, options: EscalationOptions = {}): Promise<void> {
  const now = Date.now()
  let nextLevel = Math.max(snapshot.level + 1, options.minLevel ?? 1)
  if (isMajorFailure(reason)) {
    nextLevel = Math.max(nextLevel, 4)
  }
  if (options.forceRestart) {
    nextLevel = MAX_LEVEL
  }
  nextLevel = Math.max(1, Math.min(MAX_LEVEL, nextLevel))

  updateSnapshot({
    level: nextLevel,
    mode: nextLevel >= MAX_LEVEL ? 'force-restart' : 'recovering',
    lastReason: reason,
    lastEventAt: now,
    failureCount: snapshot.failureCount + 1,
    recoveryPendingSince: now,
  })

  console.warn(`[HMR Inspector] escalation level=${nextLevel}, reason=${reason}`)
  await performRecovery(nextLevel, reason)
}

function setupWindowErrorHooks(): void {
  window.addEventListener('error', (event) => {
    const message = String((event as ErrorEvent).message ?? '')
    if (!message) return

    if (
      /failed to fetch dynamically imported module/i.test(message) ||
      /loading chunk [\d]+ failed/i.test(message) ||
      /cannot apply hmr update/i.test(message)
    ) {
      void escalate(`window:error:${message}`, { minLevel: 4 })
    }
  })

  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason
    const text =
      reason instanceof Error
        ? reason.message
        : typeof reason === 'string'
          ? reason
          : JSON.stringify(reason)

    if (
      /failed to fetch dynamically imported module/i.test(text) ||
      /loading chunk [\d]+ failed/i.test(text)
    ) {
      void escalate(`window:unhandledrejection:${text}`, { minLevel: 4 })
    }
  })
}

function setupHotHooks(): void {
  const hot = import.meta.hot
  if (!hot) return

  hot.on('vite:afterUpdate', () => {
    markHmrHealthy('HMR update applied')
  })

  hot.on('vite:error', (data: any) => {
    const message = data?.err?.message ?? 'unknown vite error'
    void escalate(`vite:error:${message}`, { minLevel: 2 })
  })

  hot.on('vite:ws:disconnect', () => {
    updateSnapshot({
      mode: 'warning',
      lastReason: 'HMR websocket disconnected',
      lastEventAt: Date.now(),
      disconnectCount: snapshot.disconnectCount + 1,
      recoveryPendingSince: Date.now(),
    })

    if (disconnectTimer) clearTimeout(disconnectTimer)
    disconnectTimer = setTimeout(() => {
      if (snapshot.recoveryPendingSince) {
        void escalate('hmr websocket disconnect timeout', { minLevel: 3 })
      }
    }, DISCONNECT_GRACE_MS)
  })

  hot.on('vite:beforeFullReload', (payload: any) => {
    const now = Date.now()
    const path = typeof payload?.path === 'string' ? payload.path : 'unknown'

    fullReloadMarks = [...fullReloadMarks, now].filter((time) => now - time < 20000)

    if (fullReloadMarks.length >= 2) {
      void escalate(`beforeFullReload repeated on ${path}`, { minLevel: 4 })
      return
    }

    updateSnapshot({
      mode: 'warning',
      lastReason: `major change detected (${path})`,
      lastEventAt: now,
      recoveryPendingSince: now,
    })
  })
}

function setupWatchdog(): void {
  if (watchdogTimer) clearInterval(watchdogTimer)
  watchdogTimer = setInterval(() => {
    if (!snapshot.recoveryPendingSince) return

    if (Date.now() - snapshot.recoveryPendingSince > RECOVERY_TIMEOUT_MS) {
      void escalate('hmr recovery timeout', {
        minLevel: Math.min(MAX_LEVEL, snapshot.level + 1),
      })
    }
  }, 2500)
}

export const hmrService = {
  init: () => {
    if (initialized) return
    initialized = true

    setupHotHooks()
    setupWindowErrorHooks()
    setupWatchdog()

    if (snapshot.level > 0) {
      scheduleStableReset()
    }
  },

  getLevel: () => snapshot.level,

  getSnapshot: (): HMRInspectorSnapshot => ({ ...snapshot }),

  subscribe: (listener: (snapshot: HMRInspectorSnapshot) => void): (() => void) => {
    listeners.add(listener)
    listener({ ...snapshot })
    return () => {
      listeners.delete(listener)
    }
  },

  clearLevel: () => {
    snapshot = { ...defaultSnapshot, lastSuccessAt: Date.now() }
    emit()
  },

  forceRestart: async (reason = 'manual force restart') => {
    await escalate(reason, { forceRestart: true })
  },

  reportHealthy: (reason = 'manual healthy signal') => {
    markHmrHealthy(reason)
  },
}
