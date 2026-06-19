export type StartupLevel = 'ok' | 'warn' | 'error' | 'unknown'

export interface StartupMonitorState {
  level: StartupLevel
  label: string
  backend: string
  browserContext: string
  phase: string
  lastCheckedAt: number | null
  message: string
}

export const INITIAL_STARTUP_MONITOR: StartupMonitorState = {
  level: 'unknown',
  label: '未檢查',
  backend: 'unknown',
  browserContext: 'unknown',
  phase: 'unknown',
  lastCheckedAt: null,
  message: '尚未取得系統啟動狀態。',
}

export function resolveStartupMonitor(
  payload: Record<string, unknown>
): StartupMonitorState {
  const backend = String(payload.backend ?? 'unknown').toLowerCase()
  const browserContext = String(
    payload.browser_context ?? 'unknown'
  ).toLowerCase()
  const phase = String(payload.phase ?? 'unknown').toLowerCase()
  const ok = payload.ok !== false

  let level: StartupLevel = 'unknown'
  let label = '未知'

  if (!ok) {
    level = 'error'
    label = '異常'
  } else if (backend === 'ready' && browserContext === 'ready') {
    level = 'ok'
    label = '已啟動'
  } else if (backend === 'ready' || browserContext === 'ready') {
    level = 'warn'
    label = '部分啟動'
  } else if (backend === 'closed' && browserContext === 'closed') {
    level = 'warn'
    label = '未啟動'
  } else if (backend.includes('fail') || browserContext.includes('fail')) {
    level = 'error'
    label = '異常'
  } else if (phase.includes('fail')) {
    level = 'error'
    label = '異常'
  } else {
    level = 'warn'
    label = '待檢查'
  }

  const message =
    String(payload.message || '').trim() || '系統啟動狀態已更新。'

  return {
    level,
    label,
    backend,
    browserContext,
    phase,
    lastCheckedAt: Date.now(),
    message,
  }
}
