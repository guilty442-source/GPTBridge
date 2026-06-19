interface CreateSandboxCardActionsInput {
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
}

export interface SandboxCardActions {
  handleIntervalChange: (minutes: number) => void
  handleStart: () => void
  handleStop: () => void
}

export function createSandboxCardActions({
  onIntervalChange,
  onStart,
  onStop,
}: CreateSandboxCardActionsInput): SandboxCardActions {
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

