import type { BusyActions } from './types'
import { createBackupCardActions } from './backup/createBackupCardActions'
import { BackupCardUI } from './backup/BackupCardUI'
import { useBackupCardState } from './backup/useBackupCardState'

interface BackupToolCardProps {
  busyActions: BusyActions
  intervalMinutes: number
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onDeleteBackupRecord: () => void
  onStop: () => void
  feedback: string
}

export function BackupToolCard({
  busyActions,
  intervalMinutes,
  onIntervalChange,
  onStart,
  onDeleteBackupRecord,
  onStop,
  feedback,
}: BackupToolCardProps) {
  const state = useBackupCardState({
    busyActions,
    feedback,
    intervalMinutes,
  })
  const actions = createBackupCardActions({
    onIntervalChange,
    onStart,
    onDeleteBackupRecord,
    onStop,
  })

  return <BackupCardUI state={state} actions={actions} />
}
