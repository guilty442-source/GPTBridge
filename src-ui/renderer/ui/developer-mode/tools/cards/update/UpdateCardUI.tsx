import { zhTW } from '@/i18n/zhTW'
import type { GlobalUpdatePlan } from '@/shared/services/globalUpdateCoordinator'
import { AutoIntervalField } from '../AutoIntervalField'
import type { UpdateCardActions } from './createUpdateCardActions'
import type { UpdateCardState } from './useUpdateCardState'

interface UpdateCardUIProps {
  state: UpdateCardState
  actions: UpdateCardActions
  nonHotChangeCount: number
  nonHotChanges: string[]
  globalUpdatePlan: GlobalUpdatePlan | null
}

export function UpdateCardUI({
  state,
  actions,
  nonHotChangeCount,
  nonHotChanges,
  globalUpdatePlan,
}: UpdateCardUIProps) {
  const strategyCounts = globalUpdatePlan
    ? Object.entries(globalUpdatePlan.counts).filter(([, count]) => count > 0)
    : []

  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{zhTW.settings.updateToolTitle}</div>
          <div className="devm-tool-desc">{zhTW.settings.updateToolDesc}</div>
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

      <div className="devm-keyval">
        <div className="devm-keyval-row">
          <span>{zhTW.settings.updateNonHotCountLabel}</span>
          <b>{nonHotChangeCount}</b>
        </div>
        <div className="devm-keyval-row">
          <span>{zhTW.settings.globalUpdateActionLabel}</span>
          <b>{globalUpdatePlan?.actionLabel || zhTW.settings.globalUpdateNoAction}</b>
        </div>
      </div>

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
        <button
          type="button"
          className="devm-tool-settings-action"
          disabled={!globalUpdatePlan?.changed || state.isRunning}
          onClick={actions.handleApplyDetectedUpdates}
        >
          {zhTW.settings.globalUpdateApplyAction}
        </button>
      </div>

      {globalUpdatePlan ? (
        <div className="devm-tool-log-list">
          <div className="devm-tool-log-item">
            {globalUpdatePlan.message}
          </div>
          {strategyCounts.map(([strategy, count]) => (
            <div key={strategy} className="devm-tool-log-item">
              {`${strategy}: ${count}`}
            </div>
          ))}
        </div>
      ) : null}

      {nonHotChanges.length > 0 ? (
        <div className="devm-tool-log-list">
          {nonHotChanges.slice(0, 8).map((item) => (
            <div key={item} className="devm-tool-log-item">
              {item}
            </div>
          ))}
        </div>
      ) : null}

      {state.feedback ? (
        <div className="devm-tool-settings-feedback">{state.feedback}</div>
      ) : null}
    </article>
  )
}
