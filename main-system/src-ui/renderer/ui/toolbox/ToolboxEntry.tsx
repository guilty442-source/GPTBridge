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

      {tools.length === 0 ? (
        <div className="empty-state">{t.empty}</div>
      ) : (
        <div className="toolbox-grid">
          {tools.map((tool) => (
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
