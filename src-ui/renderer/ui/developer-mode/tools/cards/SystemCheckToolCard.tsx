import { createSystemCheckCardActions } from './system-check/createSystemCheckCardActions'
import { SystemCheckCardUI } from './system-check/SystemCheckCardUI'
import { useSystemCheckCardState } from './system-check/useSystemCheckCardState'

type StartupLevel = 'ok' | 'warn' | 'error' | 'unknown'

interface StartupMonitorState {
  level: StartupLevel
  label: string
  backend: string
  browserContext: string
  lastCheckedAt: number | null
  message: string
}

interface SystemCheckToolCardProps {
  startupChecking: boolean
  startupMonitor: StartupMonitorState
  onCheckSystemStartup: () => void
  onStop: () => void
}

export function SystemCheckToolCard({
  startupChecking,
  startupMonitor,
  onCheckSystemStartup,
  onStop,
}: SystemCheckToolCardProps) {
  const state = useSystemCheckCardState({
    startupChecking,
    startupMonitor,
  })
  const actions = createSystemCheckCardActions({
    onCheckSystemStartup,
    onStop,
  })

  return <SystemCheckCardUI state={state} actions={actions} />
}

