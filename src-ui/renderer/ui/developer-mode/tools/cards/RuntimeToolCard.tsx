import type { ToolAction, ToolRuntimeState } from '../types'

interface RuntimeToolCardProps {
  tool: ToolRuntimeState
  onToolAction: (toolId: string, action: ToolAction) => void
  allowStartWhileActive?: boolean
  startLabel?: string
  stopLabel?: string
}

function statusLabel(status: ToolRuntimeState['status']): string {
  if (status === 'running') return '運作中'
  if (status === 'starting') return '啟動中'
  if (status === 'stopping') return '停止中'
  if (status === 'error') return '異常'
  return '未啟動'
}

function statusTone(status: ToolRuntimeState['status']): 'ok' | 'warn' | 'error' {
  if (status === 'running') return 'ok'
  if (status === 'error') return 'error'
  return 'warn'
}

function formatClock(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

function formatBytes(size: number | undefined): string {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) {
    return ''
  }
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`
  }
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`
}

export function RuntimeToolCard({
  tool,
  onToolAction,
  allowStartWhileActive = false,
  startLabel = '啟動',
  stopLabel = '停止',
}: RuntimeToolCardProps) {
  const active = tool.status === 'running' || tool.status === 'starting'
  const startDisabled = active && !allowStartWhileActive
  const folderPath = tool.folderPath?.trim()
  const projectSize = formatBytes(tool.projectSizeBytes)
  const projectSizeLabel = projectSize || (folderPath ? '等待同步' : '')

  return (
    <article className="devm-tool-card">
      <div className="devm-tool-top">
        <div>
          <div className="devm-tool-name">{tool.name}</div>
          <div className="devm-tool-desc">{tool.summary}</div>
        </div>
        <span className={`devm-pill devm-pill--${statusTone(tool.status)}`}>
          <span className="devm-pill-dot" />
          {statusLabel(tool.status)}
        </span>
      </div>

      {folderPath || projectSizeLabel ? (
        <dl className="devm-tool-details">
          {folderPath ? (
            <div className="devm-tool-detail-row">
              <dt>資料夾路徑</dt>
              <dd title={folderPath}>{folderPath}</dd>
            </div>
          ) : null}
          {projectSizeLabel ? (
            <div className="devm-tool-detail-row">
              <dt>專案檔案大小</dt>
              <dd>{projectSizeLabel}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="devm-tool-meta">
        <span className="devm-tool-time">
          {tool.note} | 更新於 {formatClock(tool.updatedAt)}
        </span>
        <div className="devm-tool-actions">
          <button
            type="button"
            className="devm-tool-start"
            disabled={startDisabled}
            onClick={() => onToolAction(tool.id, 'start')}
          >
            {startLabel}
          </button>
          <button
            type="button"
            className="devm-tool-stop"
            disabled={!active}
            onClick={() => onToolAction(tool.id, 'stop')}
          >
            {stopLabel}
          </button>
        </div>
      </div>
    </article>
  )
}
