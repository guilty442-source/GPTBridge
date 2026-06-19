import { zhTW } from '@/i18n/zhTW'
import { AutoIntervalField } from '../AutoIntervalField'
import type { SandboxCardActions } from './createSandboxCardActions'
import type { SandboxCardState } from './useSandboxCardState'

interface SandboxCardUIProps {
  state: SandboxCardState
  actions: SandboxCardActions
}

export function SandboxCardUI({ state, actions }: SandboxCardUIProps) {
  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{zhTW.settings.sandboxToolTitle}</div>
          <div className="devm-tool-desc">{zhTW.settings.sandboxToolDesc}</div>
        </div>
        <span className={`devm-pill devm-pill--${state.statusTone}`}>
          <span className="devm-pill-dot" />
          {state.statusLabel}
        </span>
      </div>

      <AutoIntervalField
        value={state.intervalMinutes}
        onChange={actions.handleIntervalChange}
      />

      <div className="devm-tool-action-row">
        <button
          type="button"
          className="devm-tool-settings-action"
          disabled={!state.canStart}
          onClick={actions.handleStart}
        >
          {state.startLabel || zhTW.settings.autoToolsStartAction}
        </button>
        <button
          type="button"
          className="devm-tool-settings-action devm-tool-settings-action--danger"
          disabled={!state.canStop}
          onClick={actions.handleStop}
        >
          {zhTW.settings.stopAction}
        </button>
      </div>

      {state.feedback ? (
        <div className="devm-tool-settings-feedback">{state.feedback}</div>
      ) : null}
    </article>
  )
}

