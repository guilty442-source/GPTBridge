import { zhTW } from '@/i18n/zhTW'
import type { SystemCheckCardActions } from './createSystemCheckCardActions'
import type { SystemCheckCardState } from './useSystemCheckCardState'

interface SystemCheckCardUIProps {
  state: SystemCheckCardState
  actions: SystemCheckCardActions
}

export function SystemCheckCardUI({ state, actions }: SystemCheckCardUIProps) {
  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{zhTW.settings.systemCheckToolTitle}</div>
          <div className="devm-tool-desc">{zhTW.settings.systemCheckToolDesc}</div>
        </div>

        <span className={`devm-pill devm-pill--${state.tone}`}>
          <span className="devm-pill-dot" />
          {state.statusLabel}
        </span>
      </div>

      <div className="devm-keyval">
        <div className="devm-keyval-row">
          <span>{zhTW.settings.systemStartupBackendLabel}</span>
          <b>{state.backend}</b>
        </div>
        <div className="devm-keyval-row">
          <span>{zhTW.settings.systemStartupBrowserLabel}</span>
          <b>{state.browserContext}</b>
        </div>
        <div className="devm-keyval-row">
          <span>{zhTW.settings.systemStartupLastCheckedLabel}</span>
          <b>{state.checkedAtLabel}</b>
        </div>
      </div>

      <div className="devm-tool-action-row">
        <button
          type="button"
          className="devm-tool-settings-action"
          disabled={!state.canCheck}
          onClick={actions.handleCheckSystemStartup}
        >
          {state.startupChecking ? '處理中...' : zhTW.settings.systemCheckToolAction}
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

      <div className="devm-tool-settings-feedback">{state.message}</div>
      <div className="devm-tool-settings-feedback">
        {zhTW.settings.systemStartupRealtimeHint}
      </div>
    </article>
  )
}

