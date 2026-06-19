import type { BusyActions, UrlConfigKey } from './types'
import { createSystemSettingsCardActions } from './system-settings/createSystemSettingsCardActions'
import { SystemSettingsCardUI } from './system-settings/SystemSettingsCardUI'
import { useSystemSettingsCardState } from './system-settings/useSystemSettingsCardState'

interface SystemSettingsCardProps {
  busyActions: BusyActions
  urlDraft: Record<UrlConfigKey, string>
  onIncreaseFont: () => void
  onDecreaseFont: () => void
  onStop: () => void
  feedback: string
}

export function SystemSettingsCard({
  busyActions,
  urlDraft,
  onIncreaseFont,
  onDecreaseFont,
  onStop,
  feedback,
}: SystemSettingsCardProps) {
  const state = useSystemSettingsCardState({
    busyActions,
    urlDraft,
    feedback,
  })
  const actions = createSystemSettingsCardActions({
    onUrlChange: () => {},
    onSaveUrl: () => {},
    onIncreaseFont,
    onDecreaseFont,
    onOpenBrowser: () => {},
    onStop,
  })

  return <SystemSettingsCardUI state={state} actions={actions} />
}


