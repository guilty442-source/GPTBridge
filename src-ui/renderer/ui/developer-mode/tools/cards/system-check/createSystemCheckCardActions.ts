interface CreateSystemCheckCardActionsInput {
  onCheckSystemStartup: () => void
  onStop: () => void
}

export interface SystemCheckCardActions {
  handleCheckSystemStartup: () => void
  handleStop: () => void
}

export function createSystemCheckCardActions({
  onCheckSystemStartup,
  onStop,
}: CreateSystemCheckCardActionsInput): SystemCheckCardActions {
  return {
    handleCheckSystemStartup: () => {
      onCheckSystemStartup()
    },
    handleStop: () => {
      onStop()
    },
  }
}

