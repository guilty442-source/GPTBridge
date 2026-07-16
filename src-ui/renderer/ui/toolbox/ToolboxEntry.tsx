import { RuntimeToolCard } from './RuntimeToolCard'
import type { ToolAction, ToolRuntimeState } from './tools/types'
import './toolbox.css'

interface ToolboxEntryProps {
  tools: ToolRuntimeState[]
  connected: boolean
  syncing: boolean
  syncedAt: number | null
  onRefresh: () => void
  onToolAction: (toolId: string, action: ToolAction) => void
}

const GROUPS = [
  {
    id: 'intelligence',
    title: 'AI 與投資',
    description: '本地與外部 AI 保持獨立，投資管家只透過已驗證連線協作。',
    toolIds: ['ai-assistant', 'local-ai', 'ai-collaboration'],
  },
  {
    id: 'automation',
    title: '自動化與資料',
    description: '每項工具擁有自己的程式目錄、資料庫、權限與自動修正程序。',
    toolIds: ['file-sorter', 'vaultly', 'project-cleaner'],
  },
] as const

function formatSyncTime(timestamp: number | null): string {
  if (!timestamp) return '尚未同步'
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
          <span className="eyebrow">APPLICATIONS</span>
          <h2 id="applications-title">獨立工具</h2>
          <p>由主程式統一啟動、停止與更新；功能與資料維持清楚邊界。</p>
        </div>
        <div className="toolbox-header__actions">
          <span className="sync-time">
            {syncing ? '正在同步…' : `同步於 ${formatSyncTime(syncedAt)}`}
          </span>
          <button
            type="button"
            className="button button--ghost"
            data-testid="refresh-tools"
            disabled={!connected || syncing}
            onClick={onRefresh}
          >
            重新整理
          </button>
        </div>
      </header>

      {tools.length === 0 ? (
        <div className="empty-state">尚未發現可用的獨立工具。</div>
      ) : (
        <div className="tool-groups">
          {GROUPS.map((group) => {
            const groupTools = group.toolIds
              .map((toolId) => tools.find((tool) => tool.id === toolId))
              .filter((tool): tool is ToolRuntimeState => Boolean(tool))
            if (groupTools.length === 0) return null

            return (
              <section className="tool-group" key={group.id}>
                <div className="tool-group__heading">
                  <h3>{group.title}</h3>
                  <p>{group.description}</p>
                </div>
                <div className="toolbox-grid">
                  {groupTools.map((tool) => (
                    <RuntimeToolCard
                      key={tool.id}
                      tool={tool}
                      connected={connected}
                      onToolAction={onToolAction}
                    />
                  ))}
                </div>
              </section>
            )
          })}
        </div>
      )}
    </section>
  )
}
