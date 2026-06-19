import type { BusyActions } from './types'
import type { GlobalUpdatePlan } from '@/shared/services/globalUpdateCoordinator'
import { createUpdateCardActions } from './update/createUpdateCardActions'
import { UpdateCardUI } from './update/UpdateCardUI'
import { useUpdateCardState } from './update/useUpdateCardState'

interface UpdateToolCardProps {
  busyActions: BusyActions
  intervalMinutes: number
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
  feedback: string
  nonHotChangeCount: number
  nonHotChanges: string[]
  globalUpdatePlan: GlobalUpdatePlan | null
  onApplyDetectedUpdates: () => void
}

export function UpdateToolCard({
  busyActions,
  intervalMinutes,
  onIntervalChange,
  onStart,
  onStop,
  feedback,
  nonHotChangeCount,
  nonHotChanges,
  globalUpdatePlan,
  onApplyDetectedUpdates,
}: UpdateToolCardProps) {
  const state = useUpdateCardState({
    busyActions,
    feedback,
    intervalMinutes,
  })
  const actions = createUpdateCardActions({
    onIntervalChange,
    onStart,
    onStop,
    onApplyDetectedUpdates,
  })

  return (
    <UpdateCardUI
      state={state}
      actions={actions}
      nonHotChangeCount={nonHotChangeCount}
      nonHotChanges={nonHotChanges}
      globalUpdatePlan={globalUpdatePlan}
    />
  )
}
