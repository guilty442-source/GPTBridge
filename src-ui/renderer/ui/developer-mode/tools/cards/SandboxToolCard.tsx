import type { BusyActions } from './types'
import { createSandboxCardActions } from './sandbox/createSandboxCardActions'
import { SandboxCardUI } from './sandbox/SandboxCardUI'
import { useSandboxCardState } from './sandbox/useSandboxCardState'

interface SandboxToolCardProps {
  busyActions: BusyActions
  intervalMinutes: number
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
  feedback: string
}

export function SandboxToolCard({
  busyActions,
  intervalMinutes,
  onIntervalChange,
  onStart,
  onStop,
  feedback,
}: SandboxToolCardProps) {
  const state = useSandboxCardState({
    busyActions,
    feedback,
    intervalMinutes,
  })
  const actions = createSandboxCardActions({
    onIntervalChange,
    onStart,
    onStop,
  })
  return <SandboxCardUI state={state} actions={actions} />
}
