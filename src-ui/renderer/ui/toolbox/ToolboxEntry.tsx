import type {
  ToolAction,
  ToolRuntimeState,
} from '@/ui/developer-mode/tools/types'
import { RuntimeToolCard } from '@/ui/developer-mode/tools/cards/RuntimeToolCard'
import { zhTW } from '@/i18n/zhTW'
import '@/ui/developer-mode/developer-mode.css'

interface ToolboxEntryProps {
  tools: ToolRuntimeState[]
  syncing: boolean
  syncedAt: number | null
  onToolAction: (toolId: string, action: ToolAction) => void
}

function formatSyncTime(timestamp: number | null): string {
  if (!timestamp) return zhTW.toolbox.syncNotReady
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

export function ToolboxEntry({
  tools,
  syncing,
  syncedAt,
  onToolAction,
}: ToolboxEntryProps) {
  return (
    <section className="devm-tool-panel">
      <header className="devm-tool-header">
        <div className="devm-tool-header-top">
          <h3>{zhTW.toolbox.title}</h3>
          <span className="devm-tool-settings-feedback">
            {syncing ? zhTW.toolbox.syncing : zhTW.toolbox.synced}
          </span>
        </div>
        <p>
          {`${zhTW.toolbox.description} ${zhTW.toolbox.lastSync}: ${formatSyncTime(
            syncedAt
          )}`}
        </p>
      </header>

      {tools.length === 0 ? (
        <div className="devm-tool-card">
          {zhTW.toolbox.empty}
        </div>
      ) : (
        <div className="devm-tool-grid">
          {tools.map((tool) => (
            <RuntimeToolCard
              key={tool.id}
              tool={tool}
              onToolAction={onToolAction}
              allowStartWhileActive
              startLabel={
                tool.windowOnly
                  ? '開啟'
                  : tool.status === 'running' || tool.status === 'starting'
                  ? '開啟視窗'
                  : zhTW.toolbox.start
              }
              stopLabel={tool.windowOnly ? '關閉' : zhTW.toolbox.stop}
            />
          ))}
        </div>
      )}
    </section>
  )
}
