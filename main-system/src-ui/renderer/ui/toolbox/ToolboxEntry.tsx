import { useMemo, useState } from 'react'
import { RuntimeToolCard } from './RuntimeToolCard'
import type { ToolAction, ToolRuntimeState } from './tools/types'
import { mainSystemLocale } from '@/locales/main-system'
import './toolbox.css'

const t = mainSystemLocale.toolbox

interface ToolboxEntryProps {
  tools: ToolRuntimeState[]
  connected: boolean
  syncing: boolean
  syncedAt: number | null
  onRefresh: () => void
  onToolAction: (toolId: string, action: ToolAction) => void
}

function formatSyncTime(timestamp: number | null): string {
  if (!timestamp) return t.notSynced
  return new Date(timestamp).toLocaleTimeString('zh-TW', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function ToolboxEntry({
  tools,
  connected,
  syncing,
  syncedAt,
  onRefresh,
  onToolAction,
}: ToolboxEntryProps) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'all' | 'running' | 'available' | 'issues'>('all')
  const visibleTools = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('zh-TW')
    return tools.filter((tool) => {
      const matchesQuery =
        !needle ||
        [tool.name, tool.id, tool.summary, tool.description]
          .filter(Boolean)
          .some((value) => String(value).toLocaleLowerCase('zh-TW').includes(needle))
      const matchesFilter =
        filter === 'all' ||
        (filter === 'running' && tool.status === 'running') ||
        (filter === 'available' && tool.launchable !== false && tool.runtimeAvailable !== false) ||
        (filter === 'issues' &&
          (tool.status === 'error' ||
            (tool.launchable !== false && tool.runtimeAvailable === false)))
      return matchesQuery && matchesFilter
    })
  }, [filter, query, tools])

  return (
    <section className="toolbox-panel" aria-labelledby="applications-title">
      <header className="toolbox-header">
        <div>
          <span className="eyebrow">{t.eyebrow}</span>
          <h2 id="applications-title">{t.title}</h2>
          <p>{t.description}</p>
        </div>
        <div className="toolbox-header__actions">
          <span className="sync-time">
            {syncing ? t.syncing : `${t.syncedAt} ${formatSyncTime(syncedAt)}`}
          </span>
          <button
            type="button"
            className="button button--ghost"
            data-testid="refresh-tools"
            disabled={syncing}
            onClick={onRefresh}
          >
            {t.refresh}
          </button>
        </div>
      </header>

      <div className="toolbox-toolbar" aria-label="工具篩選">
        <label className="tool-search">
          <span className="sr-only">搜尋工具</span>
          <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none">
            <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.8" />
            <path d="m16.5 16.5 4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜尋工具或功能"
          />
          {query && (
            <button type="button" onClick={() => setQuery('')} aria-label="清除搜尋">×</button>
          )}
        </label>
        <div className="tool-filters" role="group" aria-label="工具狀態">
          {([
            ['all', '全部'],
            ['running', t.statusRunning],
            ['available', `可${t.start}`],
            ['issues', '需處理'],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={filter === value ? 'is-active' : ''}
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="tool-result-count">顯示 {visibleTools.length} / {tools.length}</span>
      </div>

      {visibleTools.length === 0 ? (
        <div className="empty-state">
          <strong>{tools.length === 0 ? t.empty : '找不到符合條件的工具'}</strong>
          {tools.length > 0 && <span>請調整搜尋文字或篩選條件。</span>}
        </div>
      ) : (
        <div className="toolbox-grid">
          {visibleTools.map((tool) => (
            <RuntimeToolCard
              key={tool.id}
              tool={tool}
              connected={connected}
              onToolAction={onToolAction}
            />
          ))}
        </div>
      )}
    </section>
  )
}
