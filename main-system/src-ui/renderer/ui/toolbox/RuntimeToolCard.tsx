import { useState } from 'react'
import type { ToolAction, ToolRuntimeState } from './tools/types'
import { formatBytes, formatProjectSize } from '@/shared/utils/format'
import { mainSystemLocale } from '@/locales/main-system'
import { Drawer } from '@/ui/drawer/Drawer'

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
  const [detailOpen, setDetailOpen] = useState(false)
  const active = tool.status === 'running' || tool.status === 'starting'
  const busy = tool.status === 'starting' || tool.status === 'stopping'
  const launchable = tool.launchable !== false && tool.runtimeAvailable !== false
  const capacity = tool.capacityBreakdown
  const hasError = tool.status === 'error'

  return (
    <article className="tool-card" data-testid={`tool-card-${tool.id}`} data-state={tool.status}>
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

      <div className="tool-card__quick-meta">
        <div className="tool-card__quick-item">
          <span>{t.folderSize}</span>
          <strong>{formatProjectSize(tool.projectSizeBytes, { fallback: t.calculating })}</strong>
        </div>
        <div className="tool-card__quick-item">
          <span>{t.dataBoundary}</span>
          <strong>{dataBoundaryLabel(tool)}</strong>
        </div>
      </div>

      {hasError && (
        <p className="tool-card__note is-error">{tool.note}</p>
      )}

      <div className="tool-card__footer">
        <button
          type="button"
          className="tool-card__detail-btn"
          onClick={() => setDetailOpen(true)}
        >
          {t.capacityBreakdown}
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>

        {!tool.lifecycleLocked && (
          <div className="tool-card__actions">
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
          </div>
        )}
      </div>

      <Drawer
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        title={tool.name}
        eyebrow={t.independentToolPrefix}
        icon={TOOL_MARK[tool.id] || tool.name.slice(0, 1)}
      >
        <div className="tool-detail">
          <p className="tool-detail__desc">{tool.description || tool.summary}</p>

          <section className="tool-detail__section">
            <h4>{t.folderSize}</h4>
            <div className="tool-detail__big-value">
              {formatProjectSize(tool.projectSizeBytes, { fallback: t.calculating })}
            </div>
            <p className="tool-detail__sub">
              {formatBytes(tool.projectSizeBytes, { exactBytes: true, fallback: t.calculating })}
              {' · '}{fileCountLabel(tool.projectFileCount)}
            </p>
          </section>

          <section className="tool-detail__section">
            <h4>{t.dataBoundary}</h4>
            <p className="tool-detail__text">{dataBoundaryLabel(tool)}</p>
          </section>

          <section className="tool-detail__section">
            <h4>{t.runtimeMode}</h4>
            <p className="tool-detail__text">{runtimeModeLabel(tool)}</p>
          </section>

          {capacity && (
            <section className="tool-detail__section">
              <h4>{t.capacityBreakdown}</h4>
              <dl className="tool-detail__capacity">
                <div>
                  <dt>{t.programCore}</dt>
                  <dd>{capacityCategoryLabel(capacity.program.sizeBytes, capacity.program.fileCount)}</dd>
                </div>
                <div>
                  <dt>{t.runtimeEnvironment}</dt>
                  <dd>{capacityCategoryLabel(capacity.runtime.sizeBytes, capacity.runtime.fileCount)}</dd>
                </div>
                <div>
                  <dt>{t.userData}</dt>
                  <dd>{capacityCategoryLabel(capacity.userData.sizeBytes, capacity.userData.fileCount)}</dd>
                </div>
                <div>
                  <dt>{t.cache}</dt>
                  <dd>{capacityCategoryLabel(capacity.cache.sizeBytes, capacity.cache.fileCount)}</dd>
                </div>
                <div>
                  <dt>{t.backups}</dt>
                  <dd>{capacityCategoryLabel(capacity.backups.sizeBytes, capacity.backups.fileCount)}</dd>
                </div>
              </dl>
            </section>
          )}

          {tool.note && (
            <section className="tool-detail__section">
              <h4>{hasError ? t.statusError : t.capacityBreakdown}</h4>
              <p className={`tool-detail__note${hasError ? ' is-error' : ''}`}>{tool.note}</p>
            </section>
          )}
        </div>
      </Drawer>
    </article>
  )
}
