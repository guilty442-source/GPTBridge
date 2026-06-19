interface CreateLogCardActionsInput {
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
}

export interface LogCardActions {
  handleIntervalChange: (minutes: number) => void
  handleStart: () => void
  handleStop: () => void
}

export function createLogCardActions({
  onIntervalChange,
  onStart,
  onStop,
}: CreateLogCardActionsInput): LogCardActions {
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
  }
}

