import { useMemo } from 'react'
import type { BusyActions } from '../types'

interface UseSandboxCardStateInput {
  busyActions: BusyActions
  feedback: string
  intervalMinutes: number
}

export interface SandboxCardState {
  isBusy: boolean
  isAutoRunning: boolean
  isRunning: boolean
  statusTone: 'ok' | 'warn'
  statusLabel: string
  startLabel: string
  canStart: boolean
  canStop: boolean
  feedback: string
  intervalMinutes: number
}

export function useSandboxCardState({
  busyActions,
  feedback,
  intervalMinutes,
}: UseSandboxCardStateInput): SandboxCardState {
  return useMemo(() => {
    const isMaintainBusy = busyActions.includes('sandbox-maintain')
    const isHealthBusy = busyActions.includes('sandbox-health')
    const isAutoRunning = busyActions.includes('sandbox-auto')
    const isBusy = isMaintainBusy || isHealthBusy
    const isRunning = isBusy || isAutoRunning

    return {
      isBusy,
      isAutoRunning,
      isRunning,
      statusTone: isRunning ? 'warn' : 'ok',
      statusLabel: isRunning ? '執行中' : '待命',
      startLabel: isBusy ? '處理中...' : isAutoRunning ? '自動執行中' : '',
      canStart: !isRunning,
      canStop: isRunning,
      feedback,
      intervalMinutes,
    }
  }, [busyActions, feedback, intervalMinutes])
}

