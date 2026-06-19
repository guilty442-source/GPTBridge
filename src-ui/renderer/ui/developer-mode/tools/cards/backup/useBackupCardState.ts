import { useMemo } from 'react'
import type { BusyActions } from '../types'

interface UseBackupCardStateInput {
  busyActions: BusyActions
  feedback: string
  intervalMinutes: number
}

export interface BackupCardState {
  isCreateBusy: boolean
  isDeleteBusy: boolean
  isAutoRunning: boolean
  isBusy: boolean
  isRunning: boolean
  statusTone: 'ok' | 'warn'
  statusLabel: string
  startLabel: string
  deleteLabel: string
  canStart: boolean
  canDelete: boolean
  canStop: boolean
  feedback: string
  intervalMinutes: number
}

export function useBackupCardState({
  busyActions,
  feedback,
  intervalMinutes,
}: UseBackupCardStateInput): BackupCardState {
  return useMemo(() => {
    const isCreateBusy = busyActions.includes('backup-record')
    const isDeleteBusy = busyActions.includes('backup-delete')
    const isAutoRunning = busyActions.includes('backup-auto')
    const isBusy = isCreateBusy || isDeleteBusy
    const isRunning = isBusy || isAutoRunning

    return {
      isCreateBusy,
      isDeleteBusy,
      isAutoRunning,
      isBusy,
      isRunning,
      statusTone: isRunning ? 'warn' : 'ok',
      statusLabel: isRunning ? '執行中' : '待命',
      startLabel: isCreateBusy ? '處理中...' : isAutoRunning ? '自動執行中' : '',
      deleteLabel: isDeleteBusy ? '處理中...' : '',
      canStart: !isRunning,
      canDelete: !isBusy,
      canStop: isRunning,
      feedback,
      intervalMinutes,
    }
  }, [busyActions, feedback, intervalMinutes])
}

