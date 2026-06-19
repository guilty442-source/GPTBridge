import { useMemo } from 'react'
import type { BusyActions } from '../types'

interface UseLogCardStateInput {
  busyActions: BusyActions
  feedback: string
  intervalMinutes: number
  records: string[]
}

export interface LogCardState {
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
  records: string[]
}

export function useLogCardState({
  busyActions,
  feedback,
  intervalMinutes,
  records,
}: UseLogCardStateInput): LogCardState {
  return useMemo(() => {
    const isExportingLogs = busyActions.includes('logs-export')
    const isExportingErrors = busyActions.includes('logs-export-errors')
    const isAutoRunning = busyActions.includes('logs-auto')
    const isBusy = isExportingLogs || isExportingErrors
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
      records,
    }
  }, [busyActions, feedback, intervalMinutes, records])
}

