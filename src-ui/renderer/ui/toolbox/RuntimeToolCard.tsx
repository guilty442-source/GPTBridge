import type { ToolAction, ToolRuntimeState } from './tools/types'
import { formatBytes } from '@/shared/utils/format'

interface Props {
  tool: ToolRuntimeState
  connected: boolean
  onToolAction: (toolId: string, action: ToolAction) => void
}

const STATUS_LABEL: Record<ToolRuntimeState['status'], string> = {
  running: '執行中',
  starting: '啟動中',
  stopping: '停止中',
  error: '需要處理',
  stopped: '已停止',
}

const TOOL_MARK: Record<string, string> = {
  'ai-assistant': '投',
  'local-ai': '星',
  'ai-collaboration': '外',
  'project-cleaner': '救',
  vaultly: '安',
  'file-sorter': '檔',
}

export function RuntimeToolCard({ tool, connected, onToolAction }: Props) {
  const active = tool.status === 'running' || tool.status === 'starting'
  const busy = tool.status === 'starting' || tool.status === 'stopping'
  const launchable = tool.launchable !== false && tool.executableExists !== false

  return (
    <article className="tool-card" data-testid={`tool-card-${tool.id}`}>
      <div className="tool-card__topline">
        <span className="tool-card__mark" aria-hidden="true">
          {TOOL_MARK[tool.id] || tool.name.slice(0, 1)}
        </span>
        <span className="status-pill" data-status={tool.status}>
          <span className="status-pill__dot" />
          {STATUS_LABEL[tool.status]}
        </span>
      </div>

      <div className="tool-card__body">
        <h3>{tool.name}</h3>
        <p>{tool.description || tool.summary}</p>
      </div>

      <dl className="tool-card__meta">
        <div>
          <dt>資料夾總大小</dt>
          <dd>{formatBytes(tool.projectSizeBytes, { exactBytes: true, fallback: '計算中' })}</dd>
        </div>
        <div>
          <dt>資料邊界</dt>
          <dd>獨立</dd>
        </div>
      </dl>

      <p className={`tool-card__note${tool.status === 'error' ? ' is-error' : ''}`}>
        {tool.note}
      </p>

      <div className="tool-card__actions">
        <button
          className="button button--primary"
          type="button"
          data-testid={`start-${tool.id}`}
          disabled={!connected || active || busy || !launchable}
          onClick={() => onToolAction(tool.id, 'start')}
        >
          {tool.status === 'starting' ? '啟動中…' : '啟動'}
        </button>
        <button
          className="button button--secondary"
          type="button"
          data-testid={`stop-${tool.id}`}
          disabled={!connected || !active || busy}
          onClick={() => onToolAction(tool.id, 'stop')}
        >
          {tool.status === 'stopping' ? '停止中…' : '停止'}
        </button>
      </div>
    </article>
  )
}
