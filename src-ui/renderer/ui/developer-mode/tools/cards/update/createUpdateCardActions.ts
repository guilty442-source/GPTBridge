interface CreateUpdateCardActionsInput {
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
  onApplyDetectedUpdates: () => void
}

export interface UpdateCardActions {
  handleIntervalChange: (minutes: number) => void
  handleStart: () => void
  handleStop: () => void
  handleApplyDetectedUpdates: () => void
}

export function createUpdateCardActions({
  onIntervalChange,
  onStart,
  onStop,
  onApplyDetectedUpdates,
}: CreateUpdateCardActionsInput): UpdateCardActions {
  return {
    handleIntervalChange: (minutes: number) => {
      onIntervalChange(minutes)
    },
    handleStart: () => {
      onStart()
    },
    handleStop: () => {
      onStop()
    },
    handleApplyDetectedUpdates: () => {
      onApplyDetectedUpdates()
    },
  }
}
