interface CreateBackupCardActionsInput {
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onDeleteBackupRecord: () => void
  onStop: () => void
}

export interface BackupCardActions {
  handleIntervalChange: (minutes: number) => void
  handleStart: () => void
  handleDeleteBackupRecord: () => void
  handleStop: () => void
}

export function createBackupCardActions({
  onIntervalChange,
  onStart,
  onDeleteBackupRecord,
  onStop,
}: CreateBackupCardActionsInput): BackupCardActions {
  return {
    handleIntervalChange: (minutes: number) => {
      onIntervalChange(minutes)
    },
    handleStart: () => {
      onStart()
    },
    handleDeleteBackupRecord: () => {
      onDeleteBackupRecord()
    },
    handleStop: () => {
      onStop()
    },
  }
}

