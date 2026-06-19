import { zhTW } from '@/i18n/zhTW'
import { AutoIntervalField } from '../AutoIntervalField'
import type { BackupCardActions } from './createBackupCardActions'
import type { BackupCardState } from './useBackupCardState'

interface BackupCardUIProps {
  state: BackupCardState
  actions: BackupCardActions
}

export function BackupCardUI({ state, actions }: BackupCardUIProps) {
  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{zhTW.settings.backupToolTitle}</div>
          <div className="devm-tool-desc">{zhTW.settings.backupToolDesc}</div>
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
          disabled={!state.canDelete}
          onClick={actions.handleDeleteBackupRecord}
        >
          {state.deleteLabel || zhTW.settings.backupDeleteAction}
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

