import { useMemo } from 'react'

type StartupLevel = 'ok' | 'warn' | 'error' | 'unknown'

interface StartupMonitorState {
  level: StartupLevel
  label: string
  backend: string
  browserContext: string
  lastCheckedAt: number | null
  message: string
}

interface UseSystemCheckCardStateInput {
  startupChecking: boolean
  startupMonitor: StartupMonitorState
}

export interface SystemCheckCardState {
  startupChecking: boolean
  tone: 'ok' | 'warn' | 'error'
  statusLabel: string
  backend: string
  browserContext: string
  checkedAtLabel: string
  message: string
  canCheck: boolean
  canStop: boolean
}

function pillTone(level: StartupLevel): 'ok' | 'warn' | 'error' {
  if (level === 'ok') return 'ok'
  if (level === 'error') return 'error'
  return 'warn'
}

function formatCheckedAt(timestamp: number | null): string {
  if (!timestamp) return '尚未檢查'
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

export function useSystemCheckCardState({
  startupChecking,
  startupMonitor,
}: UseSystemCheckCardStateInput): SystemCheckCardState {
  return useMemo(
    () => ({
      startupChecking,
      tone: pillTone(startupMonitor.level),
      statusLabel: startupChecking ? '檢查中' : startupMonitor.label,
      backend: startupMonitor.backend,
      browserContext: startupMonitor.browserContext,
      checkedAtLabel: formatCheckedAt(startupMonitor.lastCheckedAt),
      message: startupMonitor.message,
      canCheck: !startupChecking,
      canStop: startupChecking,
    }),
    [startupChecking, startupMonitor]
  )
}

