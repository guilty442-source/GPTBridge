import { useMemo } from 'react'
import type { BusyActions } from '../types'

interface UseUpdateCardStateInput {
  busyActions: BusyActions
  feedback: string
  intervalMinutes: number
}

export interface UpdateCardState {
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

export function useUpdateCardState({
  busyActions,
  feedback,
  intervalMinutes,
}: UseUpdateCardStateInput): UpdateCardState {
  return useMemo(() => {
    const isBusy = busyActions.includes('update-refresh')
    const isApplying = busyActions.includes('update-apply')
    const isAutoRunning = busyActions.includes('update-auto')
    const isRunning = isBusy || isApplying || isAutoRunning

    return {
      isBusy,
      isAutoRunning,
      isRunning,
      statusTone: isRunning ? 'warn' : 'ok',
      statusLabel: isRunning ? '執行中' : '待命',
      startLabel: isBusy || isApplying ? '處理中...' : isAutoRunning ? '自動執行中' : '',
      canStart: !isRunning,
      canStop: isRunning,
      feedback,
      intervalMinutes,
    }
  }, [busyActions, feedback, intervalMinutes])
}
