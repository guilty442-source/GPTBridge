import { zhTW } from '@/i18n/zhTW'
import type { SystemSettingsCardActions } from './createSystemSettingsCardActions'
import type { SystemSettingsCardState } from './useSystemSettingsCardState'

interface SystemSettingsCardUIProps {
  state: SystemSettingsCardState
  actions: SystemSettingsCardActions
}

export function SystemSettingsCardUI({
  state,
  actions,
}: SystemSettingsCardUIProps) {
  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{zhTW.devTabs.systemSettings}</div>
          <div className="devm-tool-desc">{zhTW.settings.systemSettingsDesc}</div>
        </div>
        <span className={`devm-pill devm-pill--${state.statusTone}`}>
          <span className="devm-pill-dot" />
          {state.statusLabel}
        </span>
      </div>

      <div className="devm-tool-action-row">
        <button
          type="button"
          className="devm-tool-settings-action"
          disabled={state.isIncreasingFont}
          onClick={actions.handleIncreaseFont}
        >
          {state.isIncreasingFont ? '處理中...' : zhTW.settings.increaseTextSize}
        </button>
        <button
          type="button"
          className="devm-tool-settings-action"
          disabled={state.isDecreasingFont}
          onClick={actions.handleDecreaseFont}
        >
          {state.isDecreasingFont ? '處理中...' : zhTW.settings.decreaseTextSize}
        </button>
        <button
          type="button"
          className="devm-tool-settings-action devm-tool-settings-action--danger"
          disabled={!state.isBusy}
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

