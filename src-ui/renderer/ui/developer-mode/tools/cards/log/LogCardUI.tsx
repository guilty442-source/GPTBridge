import { zhTW } from '@/i18n/zhTW'
import { AutoIntervalField } from '../AutoIntervalField'
import type { LogCardActions } from './createLogCardActions'
import type { LogCardState } from './useLogCardState'

interface LogCardUIProps {
  state: LogCardState
  actions: LogCardActions
}

export function LogCardUI({ state, actions }: LogCardUIProps) {
  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{zhTW.settings.logToolTitle}</div>
          <div className="devm-tool-desc">{zhTW.settings.logToolDesc}</div>
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

      <div className="devm-tool-log-list">
        {state.records.length === 0 ? (
          <div className="devm-tool-log-item">目前沒有操作紀錄。</div>
        ) : (
          state.records.map((item, index) => (
            <div key={`${item}-${index}`} className="devm-tool-log-item">
              {item}
            </div>
          ))
        )}
      </div>
    </article>
  )
}

