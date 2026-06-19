import type { BusyActions } from './types'
import { createLogCardActions } from './log/createLogCardActions'
import { LogCardUI } from './log/LogCardUI'
import { useLogCardState } from './log/useLogCardState'

interface LogToolCardProps {
  busyActions: BusyActions
  intervalMinutes: number
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
  feedback: string
  records: string[]
}

export function LogToolCard({
  busyActions,
  intervalMinutes,
  onIntervalChange,
  onStart,
  onStop,
  feedback,
  records,
}: LogToolCardProps) {
  const state = useLogCardState({
    busyActions,
    feedback,
    intervalMinutes,
    records,
  })
  const actions = createLogCardActions({
    onIntervalChange,
    onStart,
    onStop,
  })

  return <LogCardUI state={state} actions={actions} />
}
