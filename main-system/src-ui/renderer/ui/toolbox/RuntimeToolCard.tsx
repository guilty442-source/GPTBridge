import type { ToolAction, ToolRuntimeState } from './tools/types'
import { formatBytes, formatProjectSize } from '@/shared/utils/format'
import { mainSystemLocale } from '@/locales/main-system'

const t = mainSystemLocale.toolbox

interface Props {
  tool: ToolRuntimeState
  connected: boolean
  onToolAction: (toolId: string, action: ToolAction) => void
}

const STATUS_LABEL: Record<ToolRuntimeState['status'], string> = {
  running: t.statusRunning,
  starting: t.statusStarting,
  stopping: t.statusStopping,
  error: t.statusError,
  stopped: t.statusStopped,
}

const TOOL_MARK: Record<string, string> = {
  'ai-assistant': '投',
  'xingcheng': '星',
  'ai-collaboration': '外',
  'project-cleaner': '救',
  vaultly: '安',
  'file-sorter': '檔',
}

function dataBoundaryLabel(tool: ToolRuntimeState): string {
  const scope = tool.dataBoundary?.databaseScope
  if (scope === 'tool-database-only') return t.toolDatabaseOnly
  if (scope) return scope
  if (tool.dataBoundary?.codeScope === 'tool-root-only') return t.toolRootOnly
  return tool.dataBoundary?.standalone ? t.independent : t.notDeclared
}

function runtimeModeLabel(tool: ToolRuntimeState): string {
  const selected = tool.automaticRuntimeMode === 'governed-source'
    ? t.governedSource
    : tool.automaticRuntimeMode === 'executable'
      ? t.executable
      : ''
  if (tool.runtimeMode === 'dual-runtime') {
    return selected ? `${t.dualRuntime}（${t.currentRuntime}：${selected}）` : t.dualRuntime
  }
  if (tool.runtimeMode === 'governed-source') return t.governedSource
  if (tool.runtimeMode === 'executable') return t.executable
  return t.notDeclared
}

function fileCountLabel(value: number | undefined): string {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? `${new Intl.NumberFormat('zh-TW').format(value)} ${t.files}`
    : t.calculating
}

function capacityCategoryLabel(sizeBytes: number, fileCount: number): string {
  return `${formatBytes(sizeBytes)} · ${fileCountLabel(fileCount)}`
}

export function RuntimeToolCard({ tool, connected, onToolAction }: Props) {
  const active = tool.status === 'running' || tool.status === 'starting'
  const busy = tool.status === 'starting' || tool.status === 'stopping'
  const launchable = tool.launchable !== false && tool.runtimeAvailable !== false
  const capacity = tool.capacityBreakdown

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
          <dt>{t.folderSize}</dt>
          <dd>
            {formatProjectSize(tool.projectSizeBytes, { fallback: t.calculating })}
            <small>
              {formatBytes(tool.projectSizeBytes, { exactBytes: true, fallback: t.calculating })}
              {' · '}{fileCountLabel(tool.projectFileCount)}
            </small>
          </dd>
        </div>
        <div>
          <dt>{t.dataBoundary}</dt>
          <dd>{dataBoundaryLabel(tool)}</dd>
        </div>
        <div>
          <dt>{t.runtimeMode}</dt>
          <dd>{runtimeModeLabel(tool)}</dd>
        </div>
      </dl>

      {capacity ? (
        <section className="tool-card__capacity" aria-label={t.capacityBreakdown}>
          <h4>{t.capacityBreakdown}</h4>
          <dl>
            <div><dt>{t.programCore}</dt><dd>{capacityCategoryLabel(capacity.program.sizeBytes, capacity.program.fileCount)}</dd></div>
            <div><dt>{t.runtimeEnvironment}</dt><dd>{capacityCategoryLabel(capacity.runtime.sizeBytes, capacity.runtime.fileCount)}</dd></div>
            <div><dt>{t.userData}</dt><dd>{capacityCategoryLabel(capacity.userData.sizeBytes, capacity.userData.fileCount)}</dd></div>
            <div><dt>{t.cache}</dt><dd>{capacityCategoryLabel(capacity.cache.sizeBytes, capacity.cache.fileCount)}</dd></div>
            <div><dt>{t.backups}</dt><dd>{capacityCategoryLabel(capacity.backups.sizeBytes, capacity.backups.fileCount)}</dd></div>
          </dl>
        </section>
      ) : null}

      <p className={`tool-card__note${tool.status === 'error' ? ' is-error' : ''}`}>
        {tool.note}
      </p>

      {!tool.lifecycleLocked ? <div className="tool-card__actions">
        <button
          className="button button--primary"
          type="button"
          data-testid={`start-${tool.id}`}
          disabled={!connected || active || busy || !launchable}
          onClick={() => onToolAction(tool.id, 'start')}
        >
          {tool.status === 'starting' ? t.starting : t.start}
        </button>
        <button
          className="button button--secondary"
          type="button"
          data-testid={`stop-${tool.id}`}
          disabled={!connected || !active || busy}
          onClick={() => onToolAction(tool.id, 'stop')}
        >
          {tool.status === 'stopping' ? t.stopping : t.stop}
        </button>
      </div> : null}
    </article>
  )
}
