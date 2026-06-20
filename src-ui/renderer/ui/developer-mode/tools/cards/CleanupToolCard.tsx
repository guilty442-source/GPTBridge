import { AutoIntervalField } from './AutoIntervalField'
import type { BusyActions } from './types'

interface CleanupToolCardProps {
  busyActions: BusyActions
  intervalMinutes: number
  onIntervalChange: (minutes: number) => void
  onStart: () => void
  onStop: () => void
  feedback: string
}

export function CleanupToolCard({
  busyActions,
  intervalMinutes,
  onIntervalChange,
  onStart,
  onStop,
  feedback,
}: CleanupToolCardProps) {
  const isRunBusy = busyActions.includes('cleanup-run')
  const isAutoRunning = busyActions.includes('cleanup-auto')
  const isRunning = isRunBusy || isAutoRunning

  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">清理工具</div>
          <div className="devm-tool-desc">
            自動預覽並隔離 Runtime 暫存、快取與過期檔案。
          </div>
        </div>
        <span className={`devm-pill devm-pill--${isRunning ? 'warn' : 'ok'}`}>
          <span className="devm-pill-dot" />
          {isRunning ? '執行中' : '待命'}
        </span>
      </div>

      <AutoIntervalField value={intervalMinutes} onChange={onIntervalChange} />

      <div className="devm-tool-action-row">
        <button
          type="button"
          className="devm-tool-settings-action"
          disabled={isRunning}
          onClick={onStart}
        >
          {isRunBusy ? '處理中...' : isAutoRunning ? '自動執行中' : '啟動自動'}
        </button>
        <button
          type="button"
          className="devm-tool-settings-action devm-tool-settings-action--danger"
          disabled={!isRunning}
          onClick={onStop}
        >
          停止
        </button>
      </div>

      {feedback ? (
        <div className="devm-tool-settings-feedback">{feedback}</div>
      ) : null}
    </article>
  )
}
